import os, json, time, glob, subprocess, urllib.request, sys
sys.path.insert(0, "/opt/agent")
from sanitize import clean, isdone

API = "https://nano-gpt.com/api/v1/chat/completions"
KEY = open("/opt/agent/apikey").read().strip()
DEFAULT_MODEL = "z-ai/glm-5.3"
Q = "/opt/agent/queue"
R = "/opt/agent/reports"
os.makedirs(Q, exist_ok=True)
os.makedirs(R, exist_ok=True)

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

while True:
    beat()
    tasks = sorted(glob.glob(Q + "/*.md"))
    if not tasks:
        time.sleep(60)
        continue
    t = tasks[0]
    task = open(t).read()
    limit = 60
    model = DEFAULT_MODEL
    for ln in task.splitlines()[:5]:
        if ln.startswith("LIMIT="):
            try: limit = int(ln.split("=")[1])
            except Exception: pass
        if ln.startswith("MODEL="):
            model = ln.split("=", 1)[1].strip()
    sysp = ("Autonomous ops agent. Project HOPE_EVA sports betting system at /opt/ORACLE_EVA, Python, SQLite, systemd, Docker. "
            "Working dir /opt/ORACLE_EVA. Reply with exactly ONE bash command for the next step, plain text only, no markdown, no tool tags. "
            "Or reply starting with DONE when finished. Never use rm -rf. Critical files /opt/agent/worker.py supervisor.py tg_bot.py: "
            "always backup .bak before edit, verify py_compile and non-zero size after edit.")
    hist = [{"role": "system", "content": sysp}, {"role": "user", "content": task}]
    log = open(R + "/" + os.path.basename(t) + ".log", "a")
    ok = False
    last_cmds = []
    for step in range(limit):
        beat()
        try:
            out = llm(hist, model)
        except Exception as e:
            log.write(f"LLM ERROR ({model}): {e}\n")
            time.sleep(10)
            try:
                out = llm(hist, model)
            except Exception as e2:
                log.write(f"LLM ERROR retry: {e2}\n")
                break
        log.write(f"STEP {step} ASSISTANT:\n{out}\n")
        if isdone(out):
            ok = True
            break
        cmd = clean(out)
        if not cmd:
            continue
        if len(last_cmds) >= 3 and cmd == last_cmds[-1] == last_cmds[-2] == last_cmds[-3]:
            log.write("LOOP_DETECTED, stopping task\n")
            break
        last_cmds.append(cmd)
        res = run(cmd)
        log.write("OUTPUT:\n" + res[-8000:] + "\n")
        hist.append({"role": "assistant", "content": out[:2000]})
        hist.append({"role": "user", "content": "Output:\n" + res[-6000:] + "\nContinue with ONE bash command or DONE."})
    try:
        subprocess.run("git add -A && git -c user.email=agent@eva -c user.name=agent commit --allow-empty -m agent-" + os.path.basename(t), shell=True, cwd="/opt/ORACLE_EVA")
    except Exception:
        pass
    newname = t + (".done" if ok else ".failed")
    try:
        os.rename(t, newname)
    except Exception:
        pass
    log.close()
