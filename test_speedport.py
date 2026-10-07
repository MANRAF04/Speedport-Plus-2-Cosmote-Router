from speedport import map_port_ranges, rpc_failed, table

ranges = map_port_ranges(psid=25, psid_len=6, offset=6)
assert len(ranges) == 63
assert ranges[0] == (1424, 1439)
assert ranges[-1] == (64912, 64927)
assert all(hi - lo == 15 for lo, hi in ranges)
assert map_port_ranges(psid=0, psid_len=0, offset=0) == [(0, 65535)]

rows = table({
    "Device.NAT.PortMapping.2.Enable": True,
    "Device.NAT.PortMapping.1.Description": "HTTPS",
    "Device.Hosts.Host.1.IPv4Address.1.IPAddress": "skip",
    "Device.NAT.PortMapping.1.IPv4Address.1.IPAddress": "nested, skip",
}, "Device.NAT.PortMapping")
assert rows == {1: {"Description": "HTTPS"}, 2: {"Enable": True}}

assert not rpc_failed({"result": {"Device.X": "1"}})
assert not rpc_failed({"result": {"code": 200, "message": "ok"}})
assert rpc_failed({"result": {"code": 500}})
assert rpc_failed({"error": {"message": "nope"}})
print("ok")
