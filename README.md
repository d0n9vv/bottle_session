bottle_session
==============
A session plugin for bottle.实现了Memcached存储session数据。

Usage Example:

``` python
import bottle
import pylibmc
from bottle_session import SessionPlugin


app = Bottle()

mc = pylibmc.Client(['127.0.0.1:11211'])
session = SessionPlugin(mc)

app.install(session)

@app.route('/')
def index(session):
    session['test'] = 'test汉字'
    session.save()
    return 'set session'

@app.route('/show_session')
def show_session(session):
    return session['test']
    
``` 