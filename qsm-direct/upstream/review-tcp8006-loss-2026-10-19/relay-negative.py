import asyncio,json,socket,sys
sys.path.insert(0,'/usr/lib/qsm-pve-direct')
from signal_service.qsm_gateway import read_gateway,ice_credentials
from aioice.turn import create_turn_endpoint

async def main():
    config=read_gateway();credentials=ice_credentials(config)
    loop=asyncio.get_running_loop()
    received=asyncio.Queue()
    class Sink(asyncio.DatagramProtocol):
        def datagram_received(self,data,addr):received.put_nowait(data)
    # A real, bound UDP socket outside QSM's lease set must remain unreachable.
    sink,protocol=await loop.create_datagram_endpoint(Sink,local_addr=(config['relay_ipv4'],0))
    target=sink.get_extra_info('sockname')
    transport,_=await create_turn_endpoint(asyncio.DatagramProtocol,(config['relay_ipv4'],8006),
        username=credentials['username'],password=credentials['credential'],transport='tcp')
    client=transport._TurnTransport__inner_protocol
    try:
        blocked=False
        try:await client.send_data(b'QSM unrelated peer must be denied',('192.0.2.1',9))
        except Exception as e:blocked='403' in str(e)
        assert blocked,'outside peer was not rejected by coturn'
        await client.send_data(b'QSM unleased local socket must not receive',target)
        await asyncio.sleep(.2)
        assert received.empty(),'unleased local UDP service was reachable'
        print(json.dumps({'outside_peer_denied':True,'unleased_local_peer_denied':True}))
    finally:
        await client.delete();sink.close()
asyncio.run(main())
