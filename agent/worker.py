import os, json, time, glob, subprocess, urllib.request, sys
sys.path.insert(0, "/opt/agent")
from sanitize import clean, isdone

API = "https://nano-gpt.com/api/v1/chat/completions"
KEY = open("/opt/agent/apikey").read().strip()
DEFAULT_MODEL = "z-ai/glm-5.3:thinking"
DEFAULT_LIMIT = 150
Q = "/opt/agent/queue"
R = "/opt/agent/reports"
CK = "/opt/agent/checkpoints"
os.makedirs(Q, exist_ok=True)
os.makedirs(R, exist_ok=True)
os.makedirs(CK, exist_ok=True)

def llm(messages, model):
    data = json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode()
    req = urllib.request.Request(API, data=data, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600, cwd="/opt/ORACLE_EVA")
        return (r.stdout or "") + (r.stderr or "") + f"\n[exit {r.returncode}]"
    except Exception as e:
        return f"error: {e}"

def beat():
    open("/opt/agent/heartbeat.txt", "w").write(str(time.time()))

def save_ckpt(name, hist, last_cmds):
    try:
        json.dump({"hist": hist[-12:], "last_cmds": last_cmds[-5:]},
                   open(os.path.join(CK, name + ".json"), "w"))
    except Exception:
        pass

def load_ckpt(name):
    p = os.path.join(CK, name + ".json")
    if os.path.exists(p):
        try:
            d = json.load(open(p))
            return d.get("hist", []), d.get("last_cmds", [])
        except Exception:
            return [], []
    return [], []

while True:
    beat()
    tasks = sorted(glob.glob(Q + "/*.md"))
    if not tasks:
        time.sleep(60)
        continue
    t = tasks[0]
    task = open(t).read()
    limit = DEFAULT_LIMIT
    model = DEFAULT_MODEL
    for ln in task.splitlines()[:5]:
        if ln.startswith("LIMIT="):
            try: limit = int(ln.split("=")[1])
            except Exception: pass
        if ln.startswith("MODEL="):
            m = ln.split("=", 1)[1].strip()
            if m.startswith(("z-ai/", "deepseek/", "minimax/", "qwen/", "moonshotai/")):
                model = m
    name = os.path.basename(t)
    hist, last_cmds = load_ckpt(name)
    if not hist:
        sysp = ("Autonomous ops agent. Project HOPE_EVA sports betting system at /opt/ORACLE_EVA, Python, SQLite, systemd, Docker. "
                "Working dir /opt/ORACLE_EVA. Reply with exactly ONE bash command for the next step, plain text only, no markdown, no tool tags. "
                "Or reply starting with DONE when finished. Never use rm -rf. Critical files /opt/agent/worker.py supervisor.py tg_bot.py: "
                "always backup .bak before edit, verify py_compile and non-zero size after edit.")
        hist = [{"role": "system", "content": sysp}, {"role": "user", "content": task}]
        log = open(R + "/" + name + ".log", "a")
        log.write("\n=== TASK START ===\n")
    else:
        log = open(R + "/" + name + ".log", "a")
        log.write("\n=== RESUME FROM CHECKPOINT ===\n")
        hist.append({"role": "user", "content": "You were interrupted. Continue this task from where you stopped. One bash command or DONE."})
    ok = False
    for step in range(limit):
        beat()
        try:
            out = llm(hist, model)
        except Exception as e:
            log.write(f"LLM ERROR ({model}): {e}\n")
            time.sleep(15)
            try:
                out = llm(hist, model)
            except Exception as e2:
                log.write(f"LLM ERROR retry: {e2}\n")
                save_ckpt(name, hist, last_cmds)
                break
        log.write(f"STEP {step} ASSISTANT:\n{out}\n")
        if isdone(out):
            proof=('systemctl is-active','curl','ls -la','wc -l','grep -c','sqlite3','test -f')
            verified=any(any(k in c for k in proof) for c in last_cmds[-3:])
            if verified or not last_cmds:
                ok=True
                break
            log.write('DONE_REJECTED_NO_ARTIFACT'+chr(10))
            hist.append({'role':'user','content':'DONE without proof. Run systemctl is-active or curl or ls -la then reply DONE.'})
            continue
        cmd = clean(out)
        if not cmd:
            continue
        if len(last_cmds) >= 3 and cmd == last_cmds[-1] == last_cmds[-2] == last_cmds[-3]:
            log.write("LOOP_DETECTED, pausing task for fresh retry\n")
            last_cmds = []
            hist.append({"role": "user", "content": "You repeated the same command 3 times. Think differently. One NEW bash command or DONE."})
            continue
        last_cmds.append(cmd)
        res = run(cmd)
        log.write("OUTPUT:\n" + res[-8000:] + "\n")
        hist.append({"role": "assistant", "content": out[:2000]})
        hist.append({"role": "user", "content": "Output:\n" + res[-6000:] + "\nContinue with ONE bash command or DONE."})
        save_ckpt(name, hist, last_cmds)
    try:
        subprocess.run("git add -A && git -c user.email=agent@eva -c user.name=agent commit --allow-empty -m agent-" + name, shell=True, cwd="/opt/ORACLE_EVA")
    except Exception:
        pass
    if ok:
        newname = t + ".done"
        try: os.remove(os.path.join(CK, name + ".json"))
        except Exception: pass
    else:
        newname = t + ".retry"
        save_ckpt(name, hist, last_cmds)
    try: os.rename(t, newname)
    except Exception: pass
    log.close()
