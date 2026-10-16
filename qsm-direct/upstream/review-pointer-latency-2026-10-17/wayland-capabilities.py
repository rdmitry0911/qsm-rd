import json,runpy,struct
k=runpy.run_path('/usr/lib/qsm-console/qsm-seat-keeper')
w=k['Wayland'](k['find_socket']('/run/qsm-display-2'))
registry=w.new_id(); w.send(1,1,struct.pack('<I',registry)); globals={}
def event(obj,op,data):
    if obj==registry and op==0:
        number,length=struct.unpack_from('<II',data)
        interface=data[8:8+length-1].decode()
        version=struct.unpack_from('<I',data,8+((length+3)&~3))[0]
        globals[interface]=version
w.roundtrip(event)
w.socket.close()
print(json.dumps(globals,sort_keys=True))
