"""
mock_gcs/app — 모의 임무시스템의 내부 제어·관측 평면 + 대시보드.

C2 공격 표면은 MAVLink UDP(mav_server)다. 별도 localhost 센서 포트는 물리 GNSS RF
환경을 GPS_INPUT으로 변환하는 시뮬레이터 어댑터이며 C2 우회로가 아니다. REST는
대시보드·채점 관측·환경 제어·방어 완화 훅 전용이다.

환경변수:
  SECURE=true|false   서명강제(미서명 명령 거부) on/off (기본 false)
  MAV_HOST/MAV_PORT   MAVLink UDP 바인드 (기본 127.0.0.1:14550)
  SENSOR_HOST/SENSOR_MAV_PORT  내부 GNSS 모사 포트 (기본 127.0.0.1:14600)
  LOG_PATH            감사 로그 (기본 logs/events.jsonl)

실행:
  cd src && SECURE=false ../.venv/bin/uvicorn mock_gcs.app:app --port 8137
  대시보드: http://localhost:8137/   ·  공격 표면: udp:14550
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .mav_server import MockGCSServer

SECURE = os.environ.get("SECURE", "false").lower() == "true"
MAV_HOST = os.environ.get("MAV_HOST", "127.0.0.1")
MAV_PORT = int(os.environ.get("MAV_PORT", "14550"))
SENSOR_HOST = os.environ.get("SENSOR_HOST", "127.0.0.1")
SENSOR_MAV_PORT = int(os.environ.get("SENSOR_MAV_PORT", "14600"))
LOG_PATH = os.environ.get("LOG_PATH", "logs/events.jsonl")

server = MockGCSServer(host=MAV_HOST, port=MAV_PORT,
                       sensor_host=SENSOR_HOST, sensor_port=SENSOR_MAV_PORT,
                       secure=SECURE, log_path=LOG_PATH)

app = FastAPI(
    title="UAV GCS (mock target) — MAVLink control plane",
    docs_url=None if SECURE else "/docs",
    redoc_url=None,
    openapi_url=None if SECURE else "/openapi.json",
)


@app.on_event("startup")
def _start():
    server.start()


@app.on_event("shutdown")
def _stop():
    server.stop()


# ─────────────────────────── 관측 ───────────────────────────
@app.get("/api/truth")
def get_truth():
    return server.truth()


@app.get("/api/events")
def get_events(n: int = 30):
    return server.recent(n)


@app.get("/api/status")
def get_status():
    return {"secure": SECURE,
            "c2_endpoint": f"udp:{MAV_HOST}:{MAV_PORT}",
            "mav_endpoint": f"udp:{MAV_HOST}:{MAV_PORT}",  # v3 호환 별칭
            "sensor_sim_endpoint": f"udp:{SENSOR_HOST}:{SENSOR_MAV_PORT}",
            "sensor_sim_scope": "localhost-only physical GNSS environment adapter",
            "clients": server.c2_link.client_count if server.c2_link else 0}


# ─────────────────────────── 방어 완화 훅(블루가 호출) ───────────────────────────
@app.post("/api/defense/mitigate")
def post_mitigate(by: str = "blue_agent"):
    server.mitigate(by=by)
    return {"defended": True, "defended_by": by, "response": "gnss_quarantine_external_nav_rtl"}


@app.post("/api/defense/safe_hold")
def post_safe_hold(by: str = "blue_agent"):
    """ExternalNav 품질 불충분 시 대체 대응: 안전 LOITER + 운용자 인계."""
    server.safe_hold(by=by)
    return {"defended": True, "defended_by": by, "response": "safe_hold_operator_review"}


@app.post("/api/_env/degrade_extnav")
def post_degrade_extnav(sigma_m: float = 25.0, source: str = "scenario_harness"):
    """독립 ExternalNav(VIO) 품질 저하 환경효과."""
    server.degrade_extnav(sigma_m=sigma_m, source=source)
    return {"external_nav_sigma_m": sigma_m}


# ─────────────────────────── 부차 벡터: RF 링크 열화(환경) ───────────────────────────
@app.post("/api/_env/link_degrade")
def post_link_degrade(quality: float = 0.1, hold_s: float = 6.0,
                      source: str = "scenario_harness"):
    server.degrade_link(quality=quality, hold_s=hold_s, source=source)
    return {"link_quality": quality, "hold_s": hold_s}


@app.post("/api/reset")
def post_reset():
    server.reset()
    return {"reset": True, "secure": SECURE}


# ─────────────────────────── 대시보드 ───────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASHBOARD_HTML.replace("__SECURE__", "true" if SECURE else "false")


_DASHBOARD_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<title>UAV GCS — mock target (MAVLink)</title><style>
 body{background:#070c14;color:#cfe3ff;font-family:ui-monospace,Menlo,monospace;margin:0;padding:18px}
 h1{font-size:16px;letter-spacing:2px;color:#7fb4ff;margin:0 0 4px}
 .sub{color:#5f7699;font-size:12px;margin-bottom:14px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 .card{background:#0c1524;border:1px solid #1d2b42;border-radius:8px;padding:14px}
 .card.op{border-color:#1f6f3f}.card.truth{border-color:#6f2020}
 .lbl{color:#6b83a6;font-size:11px;text-transform:uppercase;letter-spacing:1px}
 .big{font-size:22px;font-weight:700;margin:2px 0 8px}
 .green{color:#38d472}.red{color:#ff5757}.amber{color:#ffc24b}
 .bar{height:12px;background:#122036;border-radius:6px;overflow:hidden;margin:4px 0 10px}
 .bar>div{height:100%;transition:width .3s}
 .row{display:flex;justify-content:space-between;font-size:13px;padding:2px 0;border-bottom:1px solid #12203688}
 .btns{margin:14px 0}.btns button{background:#16233a;color:#cfe3ff;border:1px solid #29406a;
   border-radius:6px;padding:7px 11px;margin-right:6px;cursor:pointer;font-family:inherit;font-size:12px}
 .btns button:hover{background:#20304e}
 .feed{background:#0c1524;border:1px solid #1d2b42;border-radius:8px;padding:10px;margin-top:14px;
   height:190px;overflow:auto;font-size:11px}
 .ev{padding:2px 0;color:#8ea6c9}.ev .t{color:#4d648a}
 .ev.inject{color:#ff9d9d}.ev.verdict{color:#7dffb0}.ev.command{color:#ffd27d}
</style></head><body>
<h1>UAV GCS — MOCK TARGET · MAVLink 2.0</h1>
<div class=sub>SECURE=<b id=sec>__SECURE__</b> · C2 공격표면=udp:14550 · 센서모사=localhost:14600 · 오퍼레이터 화면 vs 지상 진실</div>
<div class=btns>
 <button onclick=defend()>🛡 블루 방어(완화)</button>
 <button onclick=degrade()>① RF 링크 열화</button>
 <button onclick=reset()>↺ 리셋</button>
 <span class=sub>· 공격은 red_agent(MAVLink)가 수행</span>
</div>
<div class=grid>
 <div class="card op"><div class=lbl>오퍼레이터 화면 (EKF 융합/GLOBAL_POSITION_INT)</div>
   <div class=big id=board>—</div>
   <div class=row><span>MODE</span><b id=mode>—</b></div>
   <div class=row><span>FAILSAFE</span><b id=fs>—</b></div>
   <div class=row><span>EKF 수평분산</span><b id=var>—</b></div>
   <div class=row><span>NAV SOURCE</span><b id=navsrc>—</b></div>
   <div class=lbl style=margin-top:8px>PLATFORM AVAILABILITY</div><div class=bar><div id=availb style="width:100%;background:#38d472"></div></div>
   <div class=row><span>PLATFORM</span><b id=avail>—</b></div>
   <div class=row><span>C2 LINK</span><b id=c2avail>—</b></div>
 </div>
 <div class="card truth"><div class=lbl>지상 진실 (오퍼레이터엔 안 보임)</div>
   <div class=big id=truthstat>—</div>
   <div class=row><span>추정-실제 편이</span><b id=bias>—</b></div>
   <div class=row><span>SPOOF 주입중</span><b id=spoof>—</b></div>
   <div class=row><span>미서명 명령 거부</span><b id=rej>—</b></div>
   <div class=row><span>C2 센서주입 거부</span><b id=srej>—</b></div>
   <div class=row><span>DEFENDED</span><b id=def>—</b></div>
   <div class=row><span>RECOVERY STATE</span><b id=dstate>—</b></div>
 </div>
</div>
<div class=feed id=feed></div>
<script>
const J=(u,o)=>fetch(u,o).then(r=>r.json());
function post(u){return J(u,{method:'POST'});}
function defend(){post('/api/defense/mitigate?by=operator');}
function degrade(){post('/api/_env/link_degrade?quality=0.1&hold_s=6');}
function reset(){post('/api/reset');}
async function loop(){
 try{
  const t=await J('/api/truth'), ev=await J('/api/events?n=16');
  mode.textContent=t.mode; fs.textContent=t.failsafe; var_.textContent=t.ekf_pos_horiz_var;
  const ab=document.getElementById('availb'); ab.style.width=t.platform_availability+'%';
  ab.style.background=t.platform_availability<50?'#ff5757':(t.platform_availability<80?'#ffc24b':'#38d472');
  avail.textContent=t.platform_availability; c2avail.textContent=t.c2_availability;
  navsrc.textContent=t.nav_source; dstate.textContent=t.defense_state;
  const healthy=t.ekf_pos_horiz_var<0.8&&t.platform_availability>=90;
  board.textContent=healthy?('● SYSTEM HEALTHY · '+t.mode):'● SYSTEM ALERT';
  board.className='big '+(healthy?'green':'amber');
  bias.textContent=t.estimate_true_bias_m+' m';
  spoof.textContent=t.spoof_active?'YES':'no'; spoof.className=t.spoof_active?'red':'';
  rej.textContent=t.rejected_unsigned;
  srej.textContent=t.rejected_c2_sensor;
  def.textContent=t.defended?('YES ('+t.defended_by+')'):'no'; def.className=t.defended?'green':'';
  truthstat.textContent=t.mission_compromised?'☠ MISSION INTEGRITY LOST':'✔ MISSION INTEGRITY OK';
  truthstat.className='big '+(t.mission_compromised?'red':'green');
  feed.innerHTML=ev.slice().reverse().map(e=>{
   let d=e.event==='telemetry'?('bias '+e.estimate_true_bias_m+'m · var '+e.ekf_pos_horiz_var+' · '+e.failsafe)
        :e.event==='inject'?(e.type+' '+(e.lat?('→'+e.lat.toFixed(5)+','+e.lon.toFixed(5)):''))
        :e.event==='verdict'?(e.verdict+' ['+e.rule+'] '+e.reason)
        :(e.event==='command'?('cmd '+e.command+' '+(e.accepted?'ACCEPT':'DENY')+(e.signed?' signed':' UNSIGNED')):JSON.stringify(e));
   return '<div class=\\"ev '+e.event+'\\"><span class=t>#'+e.seq+' '+e.event+'</span> '+d+'</div>';
  }).join('');
 }catch(e){}
 setTimeout(loop,400);
}
const var_=document.getElementById('var');
loop();
</script></body></html>"""
