"""Proxmox VE: import guests as hosts and report drift (read-only), and, when allowed,
create the guests you planned in the inventory (VM from a cloud-init template, or LXC)."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from urllib.parse import quote

from labdex.plugins.base import Change, PluginError
from labdex.plugins.base import Plugin as Base

PVE_OUI = "bc:24:11"  # what Proxmox itself uses for generated MACs
DISK_KEYS = ("scsi0", "virtio0", "sata0", "ide0")
MAC_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.I)


def _mac(s: str) -> str:
    m = MAC_RE.search(s or "")
    return m.group(1).lower() if m else ""


def _kv(s: str) -> dict:
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class _Partial(PluginError):
    """The guest exists in Proxmox, but a later step failed."""

    def __init__(self, vmid: int, msg: str):
        super().__init__(msg)
        self.vmid = vmid


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    return not (a.is_loopback or a.is_link_local or a.is_multicast)


class Plugin(Base):
    # ------------------------------------------------------------------ API
    def _api(self):
        s = self.settings
        if not s.get("url"):
            raise PluginError("set the URL first")
        return self.ctx.http(s["url"].rstrip("/") + "/api2/json/", verify=bool(s.get("verify_tls", True)), timeout=15,
                             headers={"Authorization": f"PVEAPIToken={s.get('token_id', '')}={s.get('token_secret', '')}"})

    def _get(self, http, path, **params):
        return (http.get(path, params=params or None).json() or {}).get("data")

    def test(self):
        http = self._api()
        nodes = self._get(http, "nodes") or []
        guests = self._get(http, "cluster/resources", type="vm") or []
        return f"{len(nodes)} node(s), {len(guests)} guest(s)"

    def guests(self) -> list[dict]:
        """Every VM and container with its NICs (MAC, bridge, VLAN tag) and addresses."""
        http = self._api()
        out = []
        for r in self._get(http, "cluster/resources", type="vm") or []:
            if r.get("template") and not self.settings.get("templates"):
                continue
            kind, node, vmid = r.get("type"), r.get("node"), r.get("vmid")
            g = {"vmid": int(vmid), "node": node, "kind": kind, "name": r.get("name") or f"{kind}-{vmid}",
                 "status": r.get("status", ""), "tags": [t for t in re.split(r"[;,\s]+", r.get("tags") or "") if t],
                 "nics": [], "ips": [], "description": ""}
            try:
                conf = self._get(http, f"nodes/{node}/{kind}/{vmid}/config") or {}
            except PluginError as exc:
                self.log(f"{g['name']}: config not readable ({exc})")
                conf = {}
            g["description"] = (conf.get("description") or "").strip().splitlines()[0][:200] if conf.get("description") else ""
            for key in sorted(k for k in conf if re.match(r"^net\d+$", k)):
                kv = _kv(conf[key])
                mac = _mac(kv.get("hwaddr", "")) if kind == "lxc" else _mac(conf[key])
                nic = {"name": kv.get("name", key), "mac": mac, "bridge": kv.get("bridge", ""), "tag": int(kv["tag"]) if kv.get("tag", "").isdigit() else None}
                g["nics"].append(nic)
                for k in ("ip", "ip6"):  # LXC static addresses
                    v = kv.get(k, "")
                    if v and v not in ("dhcp", "auto", "manual") and _usable(v):
                        g["ips"].append(v.split("/")[0])
            if g["status"] == "running":
                g["ips"] += self._runtime_ips(http, g)
            g["ips"] = list(dict.fromkeys(g["ips"]))
            out.append(g)
        return sorted(out, key=lambda x: x["vmid"])

    def _runtime_ips(self, http, g) -> list[str]:
        ips = []
        try:
            if g["kind"] == "lxc":
                for i in self._get(http, f"nodes/{g['node']}/lxc/{g['vmid']}/interfaces") or []:
                    for k in ("inet", "inet6"):
                        if i.get(k) and i.get("name") != "lo" and _usable(i[k]):
                            ips.append(i[k].split("/")[0])
            elif self.settings.get("guest_agent", True):
                res = (self._get(http, f"nodes/{g['node']}/qemu/{g['vmid']}/agent/network-get-interfaces") or {}).get("result") or []
                for i in res:
                    if i.get("name") == "lo":
                        continue
                    for a in i.get("ip-addresses") or []:
                        if _usable(a.get("ip-address", "")):
                            ips.append(a["ip-address"])
        except PluginError:
            pass  # no agent, or not running: addresses stay unknown
        return ips

    # ------------------------------------------------------------- linking
    def _links(self) -> dict[int, str]:
        """vmid → host name, from the host settings of this instance."""
        from labdex.models import HostModule, db
        out = {}
        self._explained = {}
        for hm in db.session.scalars(db.select(HostModule).where(HostModule.integration_id == self.ctx.integration.id)):
            vmid = (hm.settings or {}).get("vmid")
            if vmid not in (None, "", 0, "0"):  # 0 = an empty field saved from the form
                out[int(vmid)] = hm.host_name
                self._explained[hm.host_name] = str((hm.settings or {}).get("explained") or "")
        return out

    def _match_existing(self, g, by_mac, match) -> str | None:
        for n in g["nics"]:
            if n["mac"] and n["mac"] in by_mac:
                return by_mac[n["mac"]]
        for ip in g["ips"]:
            h = match(ip)
            if h:
                return h
        return match(g["name"])

    # -------------------------------------------------------------- import
    def _host_payload(self, g: dict) -> dict:
        from labdex import hostnames, naming
        from labdex.discovery import network_of as _net_of
        from labdex.models import Network, db

        nets = {n.name: n for n in db.session.scalars(db.select(Network))}

        def _network_of(ip):
            n = _net_of(ip)
            return nets.get(n.name) if n is not None else None
        match = self.ctx.match_host()
        node_host = match(g["node"])
        name_ok = bool(naming.hostname_re().match(g["name"]))
        payload = {"status": "active" if g["status"] == "running" else "offline",
                   "function": g["description"] or g["name"], "runs_on": node_host or "", "tags": g["tags"][:8]}
        if name_ok:
            payload["name"] = g["name"]
        else:
            role = re.sub(r"[^a-z0-9]", "", g["name"].lower())[:20] or f"guest{g['vmid']}"
            nxt = hostnames.next_name(self.settings.get("category") or "srv", "ct" if g["kind"] == "lxc" else "vm", role)
            payload["name"] = nxt["name"]
            fun = re.sub(r"[^a-z0-9-]", "-", g["name"].lower()).strip("-")
            if naming.FUN_RE.match(fun or ""):
                payload["fun_names"] = [fun]
        ifaces, used_ips = [], set()
        for n in g["nics"]:
            iface = {"mac": n["mac"]}
            net = None
            for ip in g["ips"]:
                if ip in used_ips:
                    continue
                cand = _network_of(ip)
                if cand is not None and (n["tag"] is None or cand.vlan in (None, n["tag"])):
                    net = cand
                    iface["ip6" if ":" in ip else "ip4"] = ip
                    used_ips.add(ip)
                    other = next((x for x in g["ips"] if x not in used_ips and _network_of(x) is cand and (":" in x) != (":" in ip)), None)
                    if other:
                        iface["ip6" if ":" in other else "ip4"] = other
                        used_ips.add(other)
                    break
            if net is None and n["tag"] is not None:
                net = db.session.scalar(db.select(Network).where(Network.vlan == n["tag"]))
            if net is not None:
                iface["network"] = net.name
                ifaces.append(iface)
        payload["interfaces"] = ifaces
        if not ifaces and node_host:
            from labdex.models import Host
            h = db.session.scalar(db.select(Host).where(Host.name == node_host))
            if h is not None:
                payload["zone"], payload["zone_pinned"] = h.zone, True
        return payload

    def plan_import_guests(self, params):
        from labdex.models import Interface, db
        links = self._links()
        by_mac = {i.mac.lower(): i.host.name for i in db.session.scalars(db.select(Interface).where(Interface.mac != "")) if i.mac}
        match = self.ctx.match_host()
        changes, planned_names = [], set()
        for g in self.guests():
            if g["vmid"] in links:
                continue
            existing = self._match_existing(g, by_mac, match)
            if existing:
                changes.append(Change("update", "link", existing, f"is {g['kind']} {g['vmid']} “{g['name']}” on {g['node']}",
                                      data={"host": existing, "vmid": g["vmid"], "node": g["node"]}))
                continue
            p = self._host_payload(g)
            while p["name"] in planned_names:  # two guests with the same role in one plan
                base, nn = p["name"].rsplit("-", 1)
                p["name"] = f"{base}-{int(nn) + 1:02d}"
            planned_names.add(p["name"])
            addrs = ", ".join(a for i in p["interfaces"] for a in (i.get("ip4"), i.get("ip6")) if a) or "no address yet"
            changes.append(Change("create", "host", p["name"], f"{g['kind']} {g['vmid']} “{g['name']}” on {g['node']} · {addrs}",
                                  after=p, data={"vmid": g["vmid"], "node": g["node"]}))
        return changes

    def apply_import_guests(self, changes, params):
        from labdex import audit, store
        from labdex.models import HostModule, db
        made = linked = 0
        for c in changes:
            if c.kind == "host":
                try:
                    h = store.save_host(dict(c.after))
                except store.ValidationError as exc:
                    db.session.rollback()
                    self.log(f"{c.name}: not imported: " + "; ".join(f"{e['field']}: {e['message']}" for e in exc.errors))
                    continue
                db.session.flush()
                audit.record("create", "host", h.name, None, store.host_to_dict(h), summary=f"imported from Proxmox ({c.data['vmid']})",
                             actor=(f"proxmox {self.ctx.name}", "system"))
                name = h.name
                made += 1
            else:
                name = c.data["host"]
                linked += 1
            hm = db.session.scalar(db.select(HostModule).where(HostModule.integration_id == self.ctx.integration.id,
                                                               HostModule.host_name == name))
            if hm is None:
                hm = HostModule(integration_id=self.ctx.integration.id, host_name=name, enabled=None, settings={})
                db.session.add(hm)
            hm.settings = {**(hm.settings or {}), "vmid": c.data["vmid"], "node": c.data["node"]}
            db.session.commit()
            self.log(f"{'imported' if c.kind == 'host' else 'linked'} {name}")
        if made:
            from labdex.plugins import jobs
            jobs.emit("host.created")
        return {"imported": made, "linked": linked}

    # --------------------------------------------------------------- drift
    def _drift(self):
        from labdex.models import Host, db
        guests = {g["vmid"]: g for g in self.guests()}
        links = self._links()
        hosts = {h.name: h for h in db.session.scalars(db.select(Host).where(Host.name.in_(list(links.values()))))}
        match = self.ctx.match_host()
        rows = []
        for vmid, hname in sorted(links.items()):
            h, g = hosts.get(hname), guests.get(vmid)
            issues = []
            if h is None:
                continue
            if g is None:
                rows.append({"host": hname, "vmid": vmid, "guest": "", "issues": ["no longer in Proxmox"]})
                continue
            if g["name"].lower() not in {h.name, *h.fun_names}:
                issues.append(f"named “{g['name']}” in Proxmox")
            gm = {n["mac"] for n in g["nics"] if n["mac"]}
            hm = {i.mac.lower() for i in h.interfaces if i.mac}
            if gm - hm:
                issues.append("MAC not in the inventory: " + ", ".join(sorted(gm - hm)))
            if hm - gm and gm:
                issues.append("MAC only in the inventory: " + ", ".join(sorted(hm - gm)))
            haddrs = {a for i in h.interfaces for a in i.all_addresses if a}
            gips = set(g["ips"])
            if gips - haddrs:
                issues.append("address not in the inventory: " + ", ".join(sorted(gips - haddrs)))
            fams = {":" in x for x in gips}  # compare only the families the guest reported
            missing = sorted(a for a in haddrs - gips if (":" in a) in fams)
            if g["status"] == "running" and missing:
                issues.append("address the guest doesn't have: " + ", ".join(missing))
            node_host = match(g["node"])
            if node_host and (h.runs_on.name if h.runs_on else "") != node_host:
                issues.append(f"runs on {node_host} (inventory: {h.runs_on.name if h.runs_on else 'nothing'})")
            if g["status"] == "stopped" and h.status == "active":
                issues.append("stopped, but active in the inventory")
            if g["status"] == "running" and h.status in ("offline", "planned"):
                issues.append(f"running, but {h.status} in the inventory")
            rows.append({"host": hname, "vmid": vmid, "guest": g["name"], "node": g["node"], "status": g["status"],
                         "issues": issues, "explained": self._explained.get(hname, "") if issues else ""})
        unknown = [g for v, g in guests.items() if v not in links]
        return rows, unknown

    def _to_discovery(self, guests: list[dict]) -> int:
        """Guests labdex has no host for go into Discovery (Unknown addresses), where they can be
        adopted as hosts; running ones count as seen now. Stopped ones without an address can't."""
        running, stopped = [], []
        for g in guests:
            macs = [n["mac"] for n in g["nics"] if n["mac"]]
            for ip in g["ips"]:
                (running if g["status"] == "running" else stopped).append(
                    {"ip": ip, "mac": macs[0] if len(macs) == 1 else "", "hostname": g["name"],
                     "vendor": f"Proxmox {'LXC' if g['kind'] == 'lxc' else 'VM'} {g['vmid']} on {g['node']}"})
        source = f"proxmox {self.ctx.name}"
        n = self.ctx.discovery.record_neighbours(source, running, mark_up=True) if running else 0
        return n + (self.ctx.discovery.record_neighbours(source, stopped, mark_up=False) if stopped else 0)

    def run_drift(self, params):
        rows, unknown = self._drift()
        if self.settings.get("discovery", True):
            n = self._to_discovery(unknown)
            if n:
                self.log(f"{n} address(es) of guests labdex doesn't know went into Discovery")
        drifting = [r for r in rows if r["issues"] and not r.get("explained")]
        for r in drifting:
            self.log(f"{r['host']}: " + "; ".join(r["issues"]))
        self.ctx.set_state("drift", {"rows": rows, "unknown": [{"vmid": g["vmid"], "name": g["name"], "node": g["node"], "kind": g["kind"],
                                                                "status": g["status"]} for g in unknown]})
        return {"guests": len(rows) + len(unknown), "linked": len(rows), "drifting": len(drifting), "not_imported": len(unknown)}

    # ---------------------------------------------------------------- views
    def page_guests(self, args):
        st = self.ctx.state.get("drift") or {}
        rows, unknown = st.get("rows", []), st.get("unknown", [])
        return {"summary": [{"label": "linked", "value": len(rows)},
                            {"label": "drifting", "value": sum(1 for r in rows if r["issues"] and not r.get("explained")), "tone": "warn"},
                            {"label": "explained", "value": sum(1 for r in rows if r["issues"] and r.get("explained"))},
                            {"label": "not in the inventory", "value": len(unknown), "tone": "warn" if unknown else "ok"}],
                "sections": [
                    {"title": "Drift", "type": "table",
                     "columns": [{"key": "host", "label": "Host"}, {"key": "vmid", "label": "ID"}, {"key": "guest", "label": "In Proxmox"},
                                 {"key": "node", "label": "Node"}, {"key": "status", "label": "State"}, {"key": "what", "label": "Differs"}],
                     "rows": [r | {"what": ("; ".join(r["issues"]) or "—") + (f" — explained: {r['explained']}" if r.get("explained") else ""),
                                   "_tone": "warn" if r["issues"] and not r.get("explained") else ""} for r in rows],
                     "empty": "Nothing linked yet: run “Import guests”."},
                    {"title": "Not in the inventory", "type": "table",
                     "columns": [{"key": "vmid", "label": "ID"}, {"key": "name", "label": "Name"}, {"key": "kind", "label": "Kind"},
                                 {"key": "node", "label": "Node"}, {"key": "status", "label": "State"}],
                     "rows": unknown, "empty": "Every guest is in the inventory."}],
                "note": "From the last “Check drift”. Import with the “Import guests” plan. labdex only changes Proxmox when "
                        "“Create guests” is on, through a plan you approve."}

    def widget_summary(self, host):
        st = self.ctx.state.get("drift")
        if not st:
            return {"kind": "list", "items": [], "sub": "run “Check drift”"}
        rows, unknown = st.get("rows", []), st.get("unknown", [])
        items = [{"label": r["host"], "value": r["issues"][0][:50], "tone": "warn", "link": f"/hosts/{r['host']}"}
                 for r in rows if r["issues"] and not r.get("explained")][:6]
        if unknown:
            items.append({"label": f"{len(unknown)} guest(s)", "value": "not in the inventory", "tone": "warn", "link": f"/integrations/{self.ctx.name}?tab=guests"})
        return {"kind": "list", "items": items, "link": f"/integrations/{self.ctx.name}?tab=guests",
                "sub": f"{len(rows)} linked, all match" if not items else f"{len(rows)} linked"}

    def widget_host(self, host):
        st = self.ctx.state.get("drift") or {}
        r = next((x for x in st.get("rows", []) if x["host"] == host["name"]), None)
        if r is None:
            return {"kind": "list", "items": [], "sub": "not linked to a Proxmox guest"}
        items = [{"label": "guest", "value": f"{r.get('guest')} ({r['vmid']})"}, {"label": "node", "value": r.get("node", "")},
                 {"label": "state", "value": r.get("status", ""), "tone": "ok" if r.get("status") == "running" else "warn"}]
        items += [{"label": "drift", "value": i, "tone": "warn"} for i in r["issues"]]
        return {"kind": "list", "items": items, "sub": "matches the inventory" if not r["issues"] else f"{len(r['issues'])} difference(s)"}


    # ------------------------------------------------------------- creation
    # A guest is created for a host that is *planned* in the inventory, enabled for this
    # instance and not linked to a guest yet. Type: the host setting, else the name's type
    # (ct → LXC, vm → VM). Node: the host it runs on, if that is a Proxmox node.
    def _require_create(self):
        if not self.settings.get("allow_create"):
            raise PluginError("guest creation is off: turn on “Create guests” in this instance's settings "
                              "(and give the token the privileges listed there)")

    def _put(self, http, path, data):
        return (http.request("PUT", path, data=data).json() or {}).get("data")

    def _post(self, http, path, data=None):
        return (http.post(path, data=data or {}).json() or {}).get("data")

    def _wait(self, http, upid, what):
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        node = upid.split(":")[1]
        deadline = time.monotonic() + int(self.settings.get("task_timeout") or 600)
        while True:
            st = self._get(http, f"nodes/{node}/tasks/{quote(upid, safe='')}/status") or {}
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise PluginError(f"{what}: Proxmox task failed: {st.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise PluginError(f"{what}: Proxmox task still running after {self.settings.get('task_timeout') or 600} s")
            time.sleep(1)

    def _node_map(self, http) -> dict[str, str]:
        """inventory host name → Proxmox node name."""
        match = self.ctx.match_host()
        out = {}
        for n in self._get(http, "nodes") or []:
            h = match(n["node"])
            out[h or n["node"]] = n["node"]
        return out

    @staticmethod
    def _guest_type(h: dict, ms: dict) -> str:
        t = (ms.get("guest_type") or "").lower()
        if t in ("vm", "qemu"):
            return "qemu"
        if t in ("lxc", "ct"):
            return "lxc"
        parts = h["name"].split("-")
        code = parts[1] if len(parts) > 2 else ""
        return {"ct": "lxc", "lxc": "lxc", "vm": "qemu"}.get(code, "")

    def _mac_for(self, host: str, idx: int, taken: set[str]) -> str:
        seed = f"{host}#{idx}"
        while True:
            h = hashlib.sha256(seed.encode()).hexdigest()
            mac = f"{PVE_OUI}:{h[0:2]}:{h[2:4]}:{h[4:6]}"
            if mac not in taken:
                taken.add(mac)
                return mac
            seed += "+"

    def _bridge_for(self, net: dict) -> tuple[str, int | None]:
        for row in self.settings.get("bridges") or []:
            if row.get("network") == net["name"]:
                tag = row.get("tag")
                return row.get("bridge") or self.settings.get("bridge") or "vmbr0", (int(tag) if str(tag or "").isdigit() else None)
        return self.settings.get("bridge") or "vmbr0", net.get("vlan")

    def _guest_spec(self, h: dict, nodes: dict, nets: dict, taken_macs: set) -> dict:
        """What the guest for host h would look like, or {"skip": why}."""
        ms = h.get("module_settings") or {}
        kind = self._guest_type(h, ms)
        if not kind:
            return {"skip": "not a VM or container: set its type in the host's Proxmox settings, or name it …-vm-… / …-ct-…"}
        node = nodes.get(h.get("runs_on") or "") or self.settings.get("default_node") or ""
        if not node:
            return {"skip": "runs on no Proxmox node: set “Runs on” to one, or a default node in the instance"}
        if node not in nodes.values():
            return {"skip": f"node {node} isn't in this Proxmox cluster"}
        template = str(ms.get("template") or (self.settings.get("lxc_template") if kind == "lxc" else self.settings.get("vm_template")) or "")
        if not template:
            return {"skip": f"no {'LXC template' if kind == 'lxc' else 'VM template'}: set one in the instance or the host"}
        if kind == "qemu" and not template.isdigit():
            return {"skip": f"the VM template is a VM id to clone (like 9000), not “{template}”"}
        nics = []
        for idx, i in enumerate(x for x in h["interfaces"] if x.get("network")):
            net = nets.get(i["network"])
            if net is None:
                continue
            bridge, tag = self._bridge_for(net)
            mac = (i.get("mac") or "").lower() or self._mac_for(h["name"], idx, taken_macs)
            ip4 = ""
            if i.get("ip4") and net.get("prefix4"):
                ip4 = f"{i['ip4']}/{net['prefix4'].split('/')[1]}"
            ip6s = i.get("ip6_all") or ([i["ip6"]] if i.get("ip6") else [])
            ip6 = f"{ip6s[0]}/{(net.get('prefix6') or '/64').split('/')[1]}" if ip6s else ""
            nics.append({"name": i.get("label") or f"eth{idx}", "mac": mac, "new_mac": not i.get("mac"), "bridge": bridge, "tag": tag,
                         "ip4": ip4, "gw4": (net.get("gateway4") or "") if (i.get("primary") or idx == 0) else "",
                         "ip6": ip6, "auto6": not ip6 and bool(net.get("prefix6")), "network": net["name"]})
        if not nics:
            return {"skip": "no interface in an inventory network"}
        d = self.settings
        return {"kind": kind, "node": node, "template": template, "name": h["name"],
                "cores": int(ms.get("cores") or d.get("cores") or 2), "memory": int(ms.get("memory") or d.get("memory") or 2048),
                "disk": int(ms.get("disk") or (d.get("disk") if kind == "lxc" else 0) or (8 if kind == "lxc" else 0)),
                "storage": ms.get("storage") or d.get("storage") or "local-lvm", "nics": nics,
                "description": h.get("function") or "", "start": bool(d.get("start", True))}

    def _plan_create(self, hosts):
        self._require_create()
        from labdex.models import Interface, db
        http = self._api()
        nodes = self._node_map(http)
        nets = {n["name"]: n for n in self.ctx.inventory.networks()}
        links = self._links()
        linked = set(links.values())
        taken = {i.mac.lower() for i in db.session.scalars(db.select(Interface).where(Interface.mac != "")) if i.mac}
        changes = []
        for h in hosts:
            if h["status"] != "planned" or h["name"] in linked or h.get("kind") == "vip":
                continue
            spec = self._guest_spec(h, nodes, nets, taken)
            if "skip" in spec:
                self.log(f"{h['name']}: skipped: {spec['skip']}")
                continue
            src = f"from {spec['template']}" if spec["kind"] == "lxc" else f"clone of {spec['template']}"
            size = f", {spec['disk']} GB" if spec["disk"] else ""
            net = "; ".join(f"{n['name']} {n['bridge']}" + (f" tag {n['tag']}" if n["tag"] else "") + f" {n['ip4'] or 'DHCP'}"
                            for n in spec["nics"])
            what = "LXC" if spec["kind"] == "lxc" else "VM"
            changes.append(Change("create", "guest", h["name"],
                                  f"{what} on {spec['node']}, {src}, {spec['cores']} cores, {spec['memory']} MB{size} on {spec['storage']} · {net}",
                                  after=spec, data={"host": h["name"]}))
        if not changes:
            self.log("nothing to create: no planned host enabled here that isn't linked to a guest yet")
        return changes

    def plan_create_guests(self, params):
        return self._plan_create(self.ctx.hosts(include_offline=True))

    def plan_create_guest(self, params):
        h = self.ctx.host(params["host"])
        if h is None:
            raise PluginError(f"{params['host']} isn't enabled for {self.ctx.name}")
        if h["status"] != "planned":
            raise PluginError(f"{h['name']} is {h['status']}: only planned hosts get a new guest")
        return self._plan_create([h])

    def apply_create_guest(self, changes, params):
        return self.apply_create_guests(changes, params)

    def _net_lxc(self, n):
        parts = [f"name={n['name']}", f"bridge={n['bridge']}", f"hwaddr={n['mac'].upper()}"]
        if n["tag"]:
            parts.append(f"tag={n['tag']}")
        parts.append(f"ip={n['ip4']}" if n["ip4"] else "ip=dhcp")
        if n["gw4"] and n["ip4"]:
            parts.append(f"gw={n['gw4'].split('/')[0]}")
        if n["ip6"]:
            parts.append(f"ip6={n['ip6']}")
        elif n["auto6"]:
            parts.append("ip6=auto")
        return ",".join(parts)

    def _create_one(self, http, spec) -> int:
        d = self.settings
        vmid = int(self._get(http, "cluster/nextid"))
        keys = (d.get("ssh_public_keys") or "").strip()
        dns = {k: v for k, v in (("nameserver", d.get("nameserver")), ("searchdomain", d.get("searchdomain"))) if v}
        if spec["kind"] == "lxc":
            body = {"vmid": vmid, "ostemplate": spec["template"], "hostname": spec["name"], "cores": spec["cores"],
                    "memory": spec["memory"], "rootfs": f"{spec['storage']}:{spec['disk'] or 8}", "unprivileged": 1,
                    "description": spec["description"], "tags": "labdex", **dns}
            if keys:
                body["ssh-public-keys"] = keys
            for n, nic in enumerate(spec["nics"]):
                body[f"net{n}"] = self._net_lxc(nic)
            self._wait(http, self._post(http, f"nodes/{spec['node']}/lxc", body), f"create {spec['name']}")
            return self._finish(http, spec, vmid)
        tpl = int(spec["template"])
        res = {r["vmid"]: r for r in self._get(http, "cluster/resources", type="vm") or []}
        if tpl not in res:
            raise PluginError(f"template {tpl} isn't in this cluster")
        tnode = res[tpl]["node"]
        clone = {"newid": vmid, "name": spec["name"], "full": 1, "storage": spec["storage"], "description": spec["description"]}
        if tnode != spec["node"]:
            clone["target"] = spec["node"]
        self._wait(http, self._post(http, f"nodes/{tnode}/qemu/{tpl}/clone", clone), f"clone {tpl} for {spec['name']}")
        try:
            self._configure_vm(http, spec, vmid, keys, dns)
        except PluginError as exc:
            raise _Partial(vmid, f"created as {vmid}, but configuring it failed: {exc}") from exc
        return self._finish(http, spec, vmid)

    def _finish(self, http, spec, vmid):
        if spec["start"]:
            try:
                self._wait(http, self._post(http, f"nodes/{spec['node']}/{spec['kind']}/{vmid}/status/start"), f"start {spec['name']}")
            except PluginError as exc:
                raise _Partial(vmid, f"created as {vmid}, but it didn't start: {exc}") from exc
        return vmid

    def _configure_vm(self, http, spec, vmid, keys, dns):
        conf = {"cores": spec["cores"], "memory": spec["memory"], "tags": "labdex", **dns}
        for n, nic in enumerate(spec["nics"]):
            conf[f"net{n}"] = f"virtio={nic['mac'].upper()},bridge={nic['bridge']}" + (f",tag={nic['tag']}" if nic["tag"] else "")
            ip = [f"ip={nic['ip4']}" if nic["ip4"] else "ip=dhcp"]
            if nic["gw4"] and nic["ip4"]:
                ip.append(f"gw={nic['gw4'].split('/')[0]}")
            if nic["ip6"]:
                ip.append(f"ip6={nic['ip6']}")
            elif nic["auto6"]:
                ip.append("ip6=auto")
            conf[f"ipconfig{n}"] = ",".join(ip)
        if keys:
            conf["sshkeys"] = quote(keys, safe="")  # Proxmox wants this one URL-encoded
        self._put(http, f"nodes/{spec['node']}/qemu/{vmid}/config", conf)
        if spec["disk"]:
            cur = self._get(http, f"nodes/{spec['node']}/qemu/{vmid}/config") or {}
            disk = next((k for k in DISK_KEYS if k in cur), None)
            m = re.search(r"size=(\d+)G", cur.get(disk, "")) if disk else None
            if disk and (m is None or int(m.group(1)) < spec["disk"]):
                self._put(http, f"nodes/{spec['node']}/qemu/{vmid}/resize", {"disk": disk, "size": f"{spec['disk']}G"})

    def apply_create_guests(self, changes, params):
        self._require_create()
        from labdex import audit, store
        from labdex.models import Host, HostModule, db
        http = self._api()
        made, failed = 0, 0
        for c in changes:
            if c.kind != "guest":
                continue
            spec = c.after
            h = db.session.scalar(db.select(Host).where(Host.name == spec["name"]))
            if h is None or h.status != "planned":
                self.log(f"{spec['name']}: no longer planned in the inventory, skipped")
                continue
            started = spec["start"]
            try:
                vmid = self._create_one(http, spec)
            except _Partial as exc:  # it exists: link it, so the next plan doesn't make a second one
                failed += 1
                self.log(f"{spec['name']}: {exc}; fix it in Proxmox, labdex linked it")
                vmid, started = exc.vmid, False
            except PluginError as exc:
                failed += 1
                self.log(f"{spec['name']}: {exc}")
                continue
            # link it, and write what Proxmox now knows back into the inventory
            hm = db.session.scalar(db.select(HostModule).where(HostModule.integration_id == self.ctx.integration.id,
                                                               HostModule.host_name == h.name))
            if hm is None:
                hm = HostModule(integration_id=self.ctx.integration.id, host_name=h.name, enabled=None, settings={})
                db.session.add(hm)
            hm.settings = {**(hm.settings or {}), "vmid": vmid, "node": spec["node"]}
            before = store.host_to_dict(h)
            after = {**before, "status": "active" if started else "offline"}
            macs = {n["network"]: n["mac"] for n in spec["nics"] if n["new_mac"]}
            after["interfaces"] = [dict(i, mac=macs.pop(i["network"], i["mac"]) if not i["mac"] else i["mac"]) for i in before["interfaces"]]
            for i in after["interfaces"]:
                i.pop("ip6_all", None)
                i.pop("zone", None)
            try:
                h = store.save_host(after, h)
            except store.ValidationError as exc:
                db.session.rollback()
                self.log(f"{spec['name']}: created as {vmid}, but the inventory wasn't updated: "
                         + "; ".join(f"{e['field']}: {e['message']}" for e in exc.errors))
                continue
            db.session.flush()
            audit.record("update", "host", h.name, before, store.host_to_dict(h), summary=f"created in Proxmox ({vmid} on {spec['node']})",
                         actor=(f"proxmox {self.ctx.name}", "system"))
            db.session.commit()
            if not started and spec["start"]:
                continue
            made += 1
            self.log(f"created {spec['name']}: {'LXC' if spec['kind'] == 'lxc' else 'VM'} {vmid} on {spec['node']}"
                     + (", started" if spec["start"] else ""))
        if made:
            from labdex.plugins import jobs
            jobs.emit("host.updated")
        if failed and not made:
            raise PluginError(f"no guest created ({failed} failed); see the log")
        return {"created": made, "failed": failed}
