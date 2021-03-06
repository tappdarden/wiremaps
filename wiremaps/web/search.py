import re
from IPy import IP

from twisted.names import client

from nevow import rend, loaders
from nevow import tags as T

from wiremaps.web.common import FragmentMixIn, RenderMixIn
from wiremaps.web.json import JsonPage

class SearchResource(rend.Page):

    addSlash = True
    docFactory = loaders.stan(T.html [ T.body [ T.p [ "Nothing here" ] ] ])

    def __init__(self, dbpool):
        self.dbpool = dbpool
        rend.Page.__init__(self)

    def childFactory(self, ctx, name):
        """Dispatch to the correct page to handle the search request.

        We can search:
         - a MAC address
         - an IP address
         - an hostname
         - a VLAN
        """
        name = name.strip()
        if re.match(r'^\d+$', name):
            vlan = int(name)
            if int(name) >= 1 and int(name) <= 4096:
                return SearchVlanResource(self.dbpool, vlan)
        if re.match(r'^(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}$', name):
            return SearchMacResource(self.dbpool, name)
        try:
            ip = IP(name)
        except ValueError:
            pass
        else:
            if "." in name:
                return SearchIPResource(self.dbpool, str(ip))
        # Should be a hostname then
        return SearchHostnameResource(self.dbpool, name)

class SearchVlanResource(JsonPage, RenderMixIn):

    def __init__(self, dbpool, vlan):
        self.vlan = vlan
        self.dbpool = dbpool
        JsonPage.__init__(self)

    def data_json(self, ctx, data):
        return [SearchVlanName(self.dbpool, self.vlan),
                SearchLocalVlan(self.dbpool, self.vlan),
                SearchRemoteVlan(self.dbpool, self.vlan)]

class SearchVlanName(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("nvlan"),
                                     data=T.directive("nvlan")))
    
    def __init__(self, dbpool, vlan):
        self.vlan = vlan
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_nvlan(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT count(vid) AS c, name "
                                    "FROM vlan_full WHERE vid=%(vid)s "
                                    "AND deleted='infinity' "
                                    "GROUP BY name ORDER BY c DESC "
                                    "LIMIT 1",
                                    {'vid': self.vlan})

    def render_nvlan(self, ctx, results):
        if not results:
            return ctx.tag["I don't know the name of this VLAN."]
        return ctx.tag["This VLAN is known as ",
                       T.span(_class="data")[results[0][1]],
                       "."]

class SearchVlan(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("nvlan"),
                                     data=T.directive("nvlan")))

    def __init__(self, dbpool, vlan):
        self.vlan = vlan
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_nvlan(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name, p.name "
                                    "FROM vlan_full v, port_full p, equipment_full e "
                                    "WHERE v.equipment=e.ip "
                                    "AND p.equipment=e.ip "
                                    "AND v.port=p.index "
                                    "AND v.vid=%(vid)s "
                                    "AND v.type=%(type)s "
                                    "AND v.deleted='infinity' "
                                    "AND p.deleted='infinity' "
                                    "AND e.deleted='infinity' "
                                    "ORDER BY v.vid, p.index",
                                    {'vid': self.vlan,
                                     'type': self.type})

    def render_nvlan(self, ctx, results):
        if not results:
            return ctx.tag["This VLAN is not known %sly." % self.type]
        ports = {}
        for equip, port in results:
            if equip not in ports:
                ports[equip] = []
            if port not in ports[equip]:
                ports[equip].append(port)
        return ctx.tag["This VLAN can be found %sly on:" % self.type,
                       T.ul [
                [ T.li[
                        T.invisible(data=equip,
                                   render=T.directive("hostname")),
                        T.small[" (on port%s " % (len(ports[equip]) > 1 and "s: " or ""),
                                T.invisible(data=ports[equip],
                                            render=T.directive("ports")),
                                ")"]
                        ] for equip in ports ]
                ] ]

class SearchLocalVlan(SearchVlan):

    type = 'local'

class SearchRemoteVlan(SearchVlan):

    type = 'remote'

class SearchMacResource(JsonPage, RenderMixIn):

    def __init__(self, dbpool, mac):
        self.mac = mac
        self.dbpool = dbpool
        JsonPage.__init__(self)

    def data_json(self, ctx, data):
        d = self.dbpool.runQueryInPast(ctx,
                                 "SELECT DISTINCT ip FROM arp_full "
                                 "WHERE mac=%(mac)s AND deleted='infinity'",
                                 {'mac': self.mac})
        d.addCallback(self.gotIPs)
        return d

    def gotIPs(self, ips):
        self.ips = [x[0] for x in ips]
        if not self.ips:
            fragment = T.span [ "I cannot find any IP associated to this MAC address" ]
        elif len(self.ips) == 1:
            fragment = T.span [ "This MAC address is associated with IP ",
                                T.invisible(data=self.ips[0],
                                            render=T.directive("ip")), "." ]
        else:
            fragment = T.span [ "This MAC address is associated with the following IPs: ",
                                T.ul [[ T.li[T.invisible(data=ip,
                                                          render=T.directive("ip")),
                                              " "] for ip in self.ips ] ]]
        fragment = FragmentMixIn(self.dbpool, docFactory=loaders.stan(fragment))
        results = [ fragment ]
        results.append(SearchMacInInterfaces(self.dbpool, self.mac))
        for ip in self.ips:
            results.append(SearchIPInEquipment(self.dbpool, ip))
            results.append(SearchIPInSonmp(self.dbpool, ip))
            results.append(SearchIPInLldp(self.dbpool, ip))
            results.append(SearchIPInCdp(self.dbpool, ip))
        results.append(SearchMacInFdb(self.dbpool, self.mac))
        return results

class SearchIPResource(JsonPage, RenderMixIn):

    def __init__(self, dbpool, ip):
        self.ip = ip
        self.dbpool = dbpool
        JsonPage.__init__(self)

    def data_json(self, ctx, data):
        d = self.dbpool.runQueryInPast(ctx,
                                 "SELECT DISTINCT mac FROM arp_full "
                                 "WHERE ip=%(ip)s AND deleted='infinity'",
                                 {'ip': self.ip})
        d.addCallback(self.gotMAC)
        return d

    def gotMAC(self, macs):
        if not macs:
            fragment = T.span [ "I cannot find any MAC associated to this IP address" ]
        else:
            self.mac = macs[0][0]
            fragment = T.span [ "This IP address ",
                                T.invisible(data=self.ip,
                                            render=T.directive("ip")),
                                " is associated with MAC ",
                                T.invisible(data=self.mac,
                                            render=T.directive("mac")), "." ]
        fragment = FragmentMixIn(self.dbpool, docFactory=loaders.stan(fragment))
        l = [ fragment,
              SearchIPInDNS(self.dbpool, self.ip),
              SearchIPInSonmp(self.dbpool, self.ip),
              SearchIPInLldp(self.dbpool, self.ip),
              SearchIPInCdp(self.dbpool, self.ip) ]
        if macs:
            l.append(SearchMacInInterfaces(self.dbpool, self.mac))
            l.append(SearchMacInFdb(self.dbpool, self.mac))
        return l

class SearchHostnameResource(JsonPage, RenderMixIn):

    def __init__(self, dbpool, name):
        self.name = name
        self.dbpool = dbpool
        JsonPage.__init__(self)

    def data_json(self, ctx, data):
        d = self.dbpool.runQueryInPast(ctx,
                                 "SELECT DISTINCT name, ip FROM equipment_full "
                                 "WHERE deleted='infinity' "
                                 "AND (name=%(name)s "
                                 "OR name ILIKE '%%'||%(name)s||'%%') "
                                 "ORDER BY name",
                                 {'name': self.name})
        d.addCallback(self.gotIP)
        return d

    def gotIP(self, ips, resolve=True):
        if not ips:
            if resolve:
                d = client.getHostByName(self.name)
                d.addCallbacks(lambda x: self.gotIP([[self.name,x]],
                                                    resolve=False),
                               lambda x: self.gotIP(None, resolve=False))
                return d
            fragment = T.span [ "I cannot find any IP for this host" ]
            fragment = FragmentMixIn(self.dbpool, docFactory=loaders.stan(fragment))
            fragments = [fragment]
        else:
            fragments = []
            for ip in ips:
                fragment = T.span [ "The hostname ",
                                    resolve and T.a(href="equipment/%s/" % ip[1],
                                        render=self.render_apiurl)[ip[0]] or \
                                        T.span(_class="data")[ip[0]],
                                    " is associated with IP ",
                                    T.invisible(data=ip[1],
                                                render=T.directive("ip")),
                                    resolve and \
                                        T.invisible[
                                            ". You can ",
                                            T.a(href="search/%s/" % ip[1],
                                                render=self.render_apiurl)
                                            ["search on it"],
                                            " to find more results." ] or "."]
                fragment = FragmentMixIn(self.dbpool, docFactory=loaders.stan(fragment))
                fragments.append(fragment)

        fragments.append(SearchHostnameInLldp(self.dbpool, self.name))
        fragments.append(SearchHostnameInCdp(self.dbpool, self.name))
        fragments.append(SearchHostnameInEdp(self.dbpool, self.name))
        fragments.append(SearchInDescription(self.dbpool, self.name))
        return fragments

class SearchInDescription(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("description"),
                                     data=T.directive("description")))

    def __init__(self, dbpool, name):
        self.dbpool = dbpool
        self.name = name
        rend.Fragment.__init__(self)

    def data_description(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT DISTINCT name, description "
                                    "FROM equipment_full "
                                    "WHERE deleted='infinity' "
                                    "AND description ILIKE '%%' || %(name)s || '%%'",
                                    {'name': self.name })

    def render_description(self, ctx, data):
        if not data:
            return ctx.tag["Nothing was found in descriptions"]
        return ctx.tag["The following descriptions match the request:",
                       T.ul[ [ T.li [
                    T.span(_class="data") [d[1]],
                    " from ",
                    T.span(data=d[0],
                           render=T.directive("hostname")), "." ]
                               for d in data ] ] ]

class SearchIPInDNS(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("dns"),
                                     data=T.directive("dns")))

    def __init__(self, dbpool, ip):
        self.ip = ip
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_dns(self, ctx, data):
        ptr = '.'.join(str(self.ip).split('.')[::-1]) + '.in-addr.arpa'
        d = client.lookupPointer(ptr)
        d.addErrback(lambda x: None)
        return d

    def render_dns(self, ctx, name):
        try:
            name = str(name[0][0].payload.name)
        except:
            return ctx.tag["This IP has no known name in DNS."]
        return ctx.tag["This IP is associated to ",
                       T.span(data=name,
                              render=T.directive("hostname")),
                       " in DNS."]

class SearchHostnameWithDiscovery(rend.Fragment, RenderMixIn):
    docFactory = loaders.stan(T.span(render=T.directive("discovery"),
                                     data=T.directive("discovery")))

    def __init__(self, dbpool, name):
        self.name = name
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_discovery(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name, p.name "
                                    "FROM equipment_full e, port_full p, " + self.table + " l "
                                    "WHERE (l.sysname=%(name)s OR l.sysname ILIKE %(name)s || '%%') "
                                    "AND l.port=p.index AND p.equipment=e.ip "
                                    "AND l.equipment=e.ip "
                                    "AND e.deleted='infinity' AND p.deleted='infinity' "
                                    "AND l.deleted='infinity' "
                                    "ORDER BY e.name", {'name': self.name})

    def render_discovery(self, ctx, data):
        if not data:
            return ctx.tag["This hostname has not been seen with %s." % self.protocolname]
        return ctx.tag["This hostname has been seen with %s: " % self.protocolname,
                       T.ul[ [ T.li [
                    "from port ",
                    T.span(_class="data") [d[1]],
                    " of ",
                    T.span(data=d[0],
                           render=T.directive("hostname")) ]  for d in data ] ] ]

class SearchHostnameInLldp(SearchHostnameWithDiscovery):
    table = "lldp_full"
    protocolname = "LLDP"
class SearchHostnameInCdp(SearchHostnameWithDiscovery):
    table = "cdp_full"
    protocolname = "CDP"
class SearchHostnameInEdp(SearchHostnameWithDiscovery):
    table = "edp_full"
    protocolname = "EDP"

class SearchMacInFdb(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("macfdb"),
                                     data=T.directive("macfdb")))

    def __init__(self, dbpool, mac):
        self.mac = mac
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_macfdb(self, ctx, data):
        # We filter out port with too many MAC
        return self.dbpool.runQueryInPast(ctx, """
SELECT DISTINCT e.name, e.ip, p.name, p.index, COUNT(f2.mac) as c
FROM fdb_full f, equipment_full e, port_full p, fdb_full f2
WHERE f.mac=%(mac)s
AND f.port=p.index AND f.equipment=e.ip
AND p.equipment=e.ip
AND (SELECT COUNT(*) FROM fdb_full WHERE port=p.index
AND equipment=e.ip AND deleted='infinity') <= 100
AND f2.port=f.port AND f2.equipment=f.equipment
AND f.deleted='infinity' AND e.deleted='infinity'
AND p.deleted='infinity' AND f2.deleted='infinity'
GROUP BY e.name, e.ip, p.name, p.index
ORDER BY c, e.name, p.index
""",
                                    {'mac': self.mac})

    def render_macfdb(self, ctx, data):
        if not data:
            return ctx.tag["I did not find this MAC on any FDB entry."]
        return ctx.tag["This MAC was found in FDB of the following equipments: ",
                       T.ul [ [ T.li[
                    T.invisible(data=l[0],
                                render=T.directive("hostname")),
                    " (", T.invisible(data=l[1],
                                      render=T.directive("ip")), ") "
                    "on port ", T.span(_class="data") [ l[2] ],
                    " (out of %d MAC address%s)" % (l[4], l[4]>1 and "es" or "") ]
                         for l in data] ] ]

class SearchMacInInterfaces(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("macif"),
                                     data=T.directive("macif")))

    def __init__(self, dbpool, mac):
        self.mac = mac
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_macif(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT DISTINCT e.name, e.ip, p.name, p.index "
                                    "FROM equipment_full e, port_full p "
                                    "WHERE p.mac=%(mac)s "
                                    "AND p.equipment=e.ip "
                                    "AND e.deleted='infinity' "
                                    "AND p.deleted='infinity' "
                                    "ORDER BY e.name, p.index",
                                    {'mac': self.mac})

    def render_macif(self, ctx, data):
        if not data:
            return ctx.tag["I did not find this MAC on any interface."]
        return ctx.tag["This MAC was found on the following interfaces: ",
                       T.ul [ [ T.li[
                    T.invisible(data=l[0],
                                render=T.directive("hostname")),
                    " (", T.invisible(data=l[1],
                                      render=T.directive("ip")), ") "
                    "interface ", T.span(_class="data") [ l[2] ] ]
                         for l in data] ] ]

class SearchIPInEquipment(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("ipeqt"),
                                     data=T.directive("ipeqt")))

    def __init__(self, dbpool, ip):
        self.ip = ip
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_ipeqt(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name FROM equipment_full e "
                                    "WHERE e.ip=%(ip)s AND e.deleted='infinity'",
                                    {'ip': self.ip})

    def render_ipeqt(self, ctx, data):
        if not data:
            return ctx.tag["The IP ",
                           T.span(data=self.ip,
                                  render=T.directive("ip")),
                           " is not owned by a known equipment."]
        return ctx.tag["The IP ",
                       T.span(data=self.ip,
                              render=T.directive("ip")),
                       " belongs to ",
                       T.span(data=data[0][0],
                              render=T.directive("hostname")),
                       "."]

class SearchIPInSonmp(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("sonmp"),
                                     data=T.directive("sonmp")))

    def __init__(self, dbpool, ip):
        self.ip = ip
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def data_sonmp(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name, p.name, s.remoteport "
                                    "FROM equipment_full e, port_full p, sonmp_full s "
                                    "WHERE s.remoteip=%(ip)s "
                                    "AND s.port=p.index AND p.equipment=e.ip "
                                    "AND s.equipment=e.ip "
                                    "AND e.deleted='infinity' AND p.deleted='infinity' "
                                    "AND s.deleted='infinity' "
                                    "ORDER BY e.name", {'ip': self.ip})

    def render_sonmp(self, ctx, data):
        if not data:
            return ctx.tag["This IP has not been seen with SONMP."]
        return ctx.tag["This IP has been seen with SONMP: ",
                       T.ul[ [ T.li [
                    "from port ",
                    T.span(_class="data") [d[1]],
                    " of ",
                    T.span(data=d[0],
                           render=T.directive("hostname")),
                    " connected to port ",
                    T.span(data=d[2], _class="data",
                           render=T.directive("sonmpport")) ] for d in data] ] ]

class SearchIPInDiscovery(rend.Fragment, RenderMixIn):

    docFactory = loaders.stan(T.span(render=T.directive("discovery"),
                                     data=T.directive("discovery")))
    discovery_name = "unknown"

    def __init__(self, dbpool, ip):
        self.ip = ip
        self.dbpool = dbpool
        rend.Fragment.__init__(self)

    def render_discovery(self, ctx, data):
        if not data:
            return ctx.tag["This IP has not been seen with %s." % self.discovery_name]
        return ctx.tag["This IP has been seen with %s: " % self.discovery_name,
                       T.ul [ [ T.li [
                    "from port ",
                    T.span(_class="data") [d[1]],
                    " of ",
                    T.span(data=d[0],
                           render=T.directive("hostname")),
                    " connected to port ",
                    T.span(_class="data") [d[2]],
                    " of ",
                    T.span(data=d[3],
                           render=T.directive("hostname"))] for d in data] ] ]

class SearchIPInLldp(SearchIPInDiscovery):

    discovery_name = "LLDP"

    def data_discovery(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name, p.name, l.portdesc, l.sysname "
                                    "FROM equipment_full e, port_full p, lldp_full l "
                                    "WHERE l.mgmtip=%(ip)s "
                                    "AND l.port=p.index AND p.equipment=e.ip "
                                    "AND l.equipment=e.ip "
                                    "AND e.deleted='infinity' "
                                    "AND p.deleted='infinity' "
                                    "AND l.deleted='infinity' "
                                    "ORDER BY e.name", {'ip': self.ip})

class SearchIPInCdp(SearchIPInDiscovery):

    discovery_name = "CDP"

    def data_discovery(self, ctx, data):
        return self.dbpool.runQueryInPast(ctx,
                                    "SELECT e.name, p.name, c.portname, c.sysname "
                                    "FROM equipment_full e, port_full p, cdp_full c "
                                    "WHERE c.mgmtip=%(ip)s "
                                    "AND c.port=p.index AND p.equipment=e.ip "
                                    "AND c.equipment=e.ip "
                                    "AND e.deleted='infinity' "
                                    "AND p.deleted='infinity' "
                                    "AND c.deleted='infinity' "
                                    "ORDER BY e.name", {'ip': self.ip})
