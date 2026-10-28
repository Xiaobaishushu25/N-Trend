"""Bounded read-only SDK transport diagnostic; never log auth or price payloads."""
import collections
import json
import os
import re
import sys
import research_swing_specialists as s
import tqsdk.auth

counts=collections.Counter()
def debug(self,msg,*args,**kwargs):
    if msg in ['websocket connected','websocket reconnected']:
        print('Transport:',msg,flush=True)
    elif msg=='websocket connection closed':
        error=str(kwargs.get('error',''))
        tags=[tag for tag in ['timeout','1006','1008','403','502','503','504','refused','certificate','getaddrinfo','SSL','proxy','handshake','reset','closed'] if tag.lower() in error.lower()]
        print('Transport closed classifications:',tags,re.findall(r'HTTP \d+|WinError \d+|Errno \d+|opening handshake|connection reset',error),flush=True)
    elif msg in ['websocket received data','websocket send data']:
        counts[msg]+=1
        if counts[msg]>10:return
        pack=kwargs.get('pack',{})
        if isinstance(pack,str):
            try:pack=json.loads(pack)
            except ValueError:pack={}
        if isinstance(pack,dict):
            print('Transport packet structure:',json.dumps(dict(event=msg,keys=list(pack),
                data_keys=[list(x) for x in pack.get('data',[])[:3] if isinstance(x,dict)],
                history_flags=[x['mdhis_more_data'] for x in pack.get('data',[]) if isinstance(x,dict) and 'mdhis_more_data' in x])),flush=True)

tqsdk.auth.ShinnyLoggerAdapter.debug=debug
if '--direct' in sys.argv:
    os.environ['NO_PROXY']=os.environ.get('NO_PROXY','')+',.shinnytech.com'
    s.acquisition.TRANSPORT['direct_provider']=True
s.older('sep_dec' if '--sep-dec' in sys.argv else 'may_aug')
