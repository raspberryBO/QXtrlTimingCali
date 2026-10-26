"""执行动画实际时钟代码，验证时间单位、暂停及重播。"""
import re
import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
if not NODE:
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    NODE = str(bundled) if bundled.exists() else None


@unittest.skipUnless(NODE, "播放时钟验证需要可用的 Node.js；项目运行不依赖它")
class TestPlayback(unittest.TestCase):
    def test_calibration_phases_and_seek(self):
        """执行真实视图代码：记录阶段不提前补偿，收尾后再回看必须还原。"""
        html = (ROOT / "docs/demo-animation-parallel.html").read_text(encoding="utf-8")
        data = json.loads(re.search(r"const DATA = (.*);$", html, re.M).group(1))
        scenes = re.search(r"const SCENES = (.*);$", html, re.M).group(1)
        functions = '\n'.join(re.search(r"function " + name + r"\([^)]*\) \{.*?\n\}", html, re.S).group()
                              for name in ("curIndex", "waveStartEdge", "vm", "calibrationState", "drawCalibration", "calibrationViewport", "fitCalibration", "waveTime", "playWave", "bindWave", "setPlay", "frame"))
        script = """
const assert = require('node:assert/strict');
const clamp01 = t => Math.max(0, Math.min(1, t));
const ease = t => t < .5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;
const fmt = (v,n) => Number(v).toFixed(n === undefined ? 1 : n);
""" + 'const DATA = ' + json.dumps(data) + ';\nconst SCENES = ' + scenes + ';\n'
        script += """
const MEAS = DATA.meas, WAVES = DATA.waves_meta, NT = MEAS.length, NW = WAVES.length, S = DATA.summary, META = DATA.meta;
const START = []; SCENES.reduce((acc,sc) => { START.push(acc); return acc + sc.dur; },0);
const stageFirstMi = {}, stageFirstGi = {};
MEAS.forEach(m => { if (!(m.stage in stageFirstMi)) { stageFirstMi[m.stage]=m.mi; stageFirstGi[m.stage]=m.gi; } });
const rows = DATA.calibration.lines.map((ln,i) => ({dataset:{i}, els:{},attrs:{},setAttribute(k,v){this.attrs[k]=v;},querySelector(cls){
  return this.els[cls] ||= {attrs:{},style:{},setAttribute(k,v){this.attrs[k]=v;}};
}}));
const element = () => ({attrs:{},style:{},setAttribute(k,v){this.attrs[k]=v;}});
let width=744, height=210;
const axis={children:[],replaceChildren(){this.children=[];},appendChild(el){this.children.push(el);}};
const svg={...element(),querySelectorAll:()=>rows,parentElement:{getBoundingClientRect:()=>({width,height})}};
const document = {querySelectorAll:()=>rows,createElementNS:element}, hint = {};
const $ = s => s==='#calibration'?svg:s==='#calAxis'?axis:hint;
const draw = () => drawCalibration(vm(T));
let T = 0;
let playing = false, rate = 1, last = 0, wavePlayback = null;
const TOTAL = START[START.length-1]+SCENES[SCENES.length-1].dur;
const playBtn = {}, requestAnimationFrame = () => {};
""" + functions + """
function sample(id,p){ wavePlayback=null;T=START[SCENES.findIndex(s=>s.id===id)]+SCENES.find(s=>s.id===id).dur*p; const v=vm(T);drawCalibration(v);return v; }
WAVES.forEach((w,gi)=>{
 const el={};bindWave(el,gi);el.onclick();
 let v=vm(T);
 assert.equal(playing,true);
 assert.equal(v.waveFull,gi,'每一波点击须进入对应测量波');
 assert.equal(v.hot,waveStartEdge(gi),'须从该波首条测量开始');
 assert.equal(v.id,w.stage==='readout_final'?'finish':'measure');
 assert.equal(v.cal.b,w.stage==='readout_final'?1:0,'收尾波须恢复前置整批补偿');
 let prevented=false,stopped=false;
 el.onkeydown({code:'Enter',preventDefault(){prevented=true;},stopPropagation(){stopped=true;}});
 assert.ok(prevented && stopped);
 assert.equal(vm(T).waveFull,gi);
 // 以不同倍速推进真实帧时钟，必须精确停在当前波末尾。
 rate=gi%2?4:.5; frame(100);
 for(let i=1;playing && i<400;i++) frame(100+i*64);
 assert.equal(playing,false,'波结束必须自动暂停');
 assert.equal(T,waveTime(gi,true),'不得越过当前波');
 assert.equal(vm(T).waveFull,gi,'结束状态须保留本波');
 assert.equal(vm(T).hot,-1,'完成后不得高亮下一波首边');
 assert.equal(vm(T).k,waveStartEdge(gi)+w.size,'本波全部结果须已展示');
 assert.equal(vm(T).waveFrac,1);
 const end=T;frame(99999);assert.equal(T,end,'暂停后时间不得推进');
 setPlay(true);assert.equal(vm(T).hot,waveStartEdge(gi),'播放应重放当前波');
});
const before=T;playWave(-1);assert.equal(T,before,'无效波不得改变状态');
assert.equal(sample('measure',.99).cal.b,0, '记录阶段不得提前补偿');
assert.equal(sample('solve',.99).cal.plan,true, '求解只能展示计划');
assert.equal(sample('write',.99).cal.c,0, '整批写回不得提前收回');
assert.equal(sample('finish',.99).cal.c,1);
sample('audit',.5);
rows.forEach((r,i)=>{
 const ln=DATA.calibration.lines[i], cal=DATA.calibration;
 assert.ok(Math.abs(r.els['.cal-end'].attrs.x1 - (cal.x0+ln.q1/cal.dom*(cal.x1-cal.x0)))<1e-8);
});
sample('truth',.5);
rows.forEach(r=>assert.equal(r.els['.cal-d3'].attrs.width,0,'回看须清除收回条'));
for (const size of [[744,210],[1250,300],[1890,400]]) {
 [width,height]=size;
 sample('audit',.5); fitCalibration();
 const layout=calibrationViewport(width,height,rows.length);
 assert.equal(layout.columns,width>=1100?4:3);
 rows.forEach((r,i)=>{
   const col=Math.floor(i/layout.rows), index=i%layout.rows;
   const cy=28+(index+.5)*layout.step;
   assert.ok(cy-layout.step*.35>=0 && cy+layout.step*.35<=height, '全部55条须在画布内');
   const end=+r.els['.cal-end'].attrs.x1;
   assert.ok(end>=layout.x0 && end<layout.x1);
   assert.ok(col*layout.cellWidth+layout.x1<=width);
   assert.ok(r.attrs.transform.startsWith('translate('));
 });
 assert.ok(axis.children.length>0);
}
"""
        result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_elapsed_pause_speed_and_replay(self):
        for filename, unit, epsilon in (
            ("demo-animation.html", 1000, 1),
            ("demo-animation-parallel.html", 1, 0.001),
        ):
            with self.subTest(filename=filename):
                html = (ROOT / "docs" / filename).read_text(encoding="utf-8")
                frame = re.search(r"function frame\(ts\) \{.*?\n\}", html, re.S).group()
                if unit == 1:
                    controls = re.search(r"function setPlay\(on\) \{.*?\n\}", html, re.S).group()
                    controls += '\nplayBtn.onclick = () => setPlay(!playing);'
                else:
                    controls = re.search(r"playBtn.onclick = .*?\n\};", html, re.S).group()
                script = """
const assert = require('node:assert/strict');
let T = 0, playing = true, rate = 1, last = 0;
let wavePlayback = null;
const playBtn = {}, bar = {}, clock = {};
const requestAnimationFrame = () => {}, draw = () => {};
const vm = x => x, fmt = x => String(x);
""" + f"const UNIT = {unit}, TOTAL = 60 * UNIT, EPS = {epsilon};\n"
                script += controls + '\n' + frame + """
frame(100);
for (let i = 1; i <= 60; i++) frame(100 + i * 1000 / 60);
assert.ok(Math.abs(T - UNIT) < 1e-6, '一秒应只推进一秒');
assert.equal(playing, true, '一秒后仍应播放');
playBtn.onclick();
const paused = T;
frame(5000);
assert.equal(T, paused, '暂停不得推进');
playBtn.onclick();
frame(10000);
assert.equal(T, paused, '恢复首帧不得累积暂停时间');
rate = 2;
frame(10020);
assert.ok(Math.abs(T - paused - .04 * UNIT) < 1e-6, '倍速须正确');
T = TOTAL - EPS; playing = true;
frame(10040);
assert.equal(playing, false, '结束须自动暂停');
playBtn.onclick();
assert.equal(T, 0, '结束后播放须从头开始');
assert.equal(playing, true);
"""
                result = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
