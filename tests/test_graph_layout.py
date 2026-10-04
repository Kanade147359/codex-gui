import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'),reason='Node required for native JS layout test')
def test_layout_fork_join_and_100_nodes():
    source=Path(__file__).resolve().parents[1]/'static/graph.js'
    script='''const {dependencyLayout}=require(process.argv[1]);
const tasks='abcde'.split('').map(id=>({id}));
const edges=[['a','b'],['a','c'],['b','d'],['c','d']].map(([parent,child])=>({parent,child}));
const first=dependencyLayout(tasks,edges);
const updated=dependencyLayout(tasks.map(t=>({...t,status:'running'})),edges);
const hundred=dependencyLayout(Array.from({length:100},(_,i)=>({id:String(i)})),[]);
console.log(JSON.stringify({first:[...first],updated:[...updated],hundred:[...hundred],vertical:[...dependencyLayout(tasks,edges,true)]}));'''
    r=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,check=True)
    data=json.loads(r.stdout);p=dict(data['first']);v=dict(data['vertical'])
    assert p['a']['x']<p['b']['x']==p['c']['x']<p['d']['x']
    assert p['b']['y']!=p['c']['y'] and p['e']['stage']==0
    assert data['first']==data['updated']
    assert v['a']['y']<v['b']['y']==v['c']['y']<v['d']['y']
    assert len({(p['x'],p['y']) for _,p in data['hundred']})==100
