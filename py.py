import requests;
KEY='asi2MBtO6ryKsZtstBptDMfJwv2Fb34b';
#r=requests.get('https://api.polygon.io/v2/aggs/ticker/MNQ/range/15/minute/2026-04-01/2026-04-03',params={'sort':'asc','limit':5,'apiKey':KEY},timeout=10);
#print(r.status_code, r.text[:300])


#r=requests.get('https://api.polygon.io/v3/reference/tickers',params={'search':'MNQ','market':'futures','apiKey':KEY},timeout=10)
#r=requests.get('https://api.polygon.io/v3/reference/tickers',params={'search':'MNQ','market':'futures','apiKey':'asi2MBtO6ryKsZtstBptDMfJwv2Fb34b'},timeout=10)
#print(r.status_code)
#print(r.text[:600])
r=requests.get('https://api.polygon.io/v3/reference/tickers',params={'search':'Micro E-mini Nasdaq','apiKey':KEY},timeout=10)
print('search broad:', r.text[:300])
# Try futures endpoint
r2=requests.get('https://api.polygon.io/vX/reference/futures/tickers',params={'apiKey':KEY},timeout=10)
print('futures ref:', r2.status_code, r2.text[:300])
