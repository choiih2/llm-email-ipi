#!/usr/bin/env python3
"""
gog_trace_shim.py  —  gogcli 래퍼(shim)

목적:
  - gog 스킬이 호출하는 gogcli 실행을 가로채(intercept) 단계별 trace를 남긴다.
  - read 계열: 그대로 실제 gogcli로 통과시키고, 반환 내용을 로깅(S1).
  - send 계열: 호출 인자를 로깅(S5)한 뒤, 모드에 따라
      dryrun  -> 실제 발송 차단, "차단됨"만 기록(S7 blocked)  [가장 안전, 기본값]
      sink    -> 수신자를 통제 sink 주소로 강제 치환 후 실제 발송(S7 redirected)
      passthrough -> 인자 그대로 실제 발송(템플릿이 이미 sink로 향할 때)

설치/사용 (자세한 건 README 참고):
  export GOG_REAL_BIN=/usr/local/bin/gogcli      # 진짜 gogcli 절대경로 (which gogcli)
  export GOG_TRACE_LOG=~/.openclaw/trace/gog_trace.jsonl
  export GOG_TRACE_MODE=dryrun                   # dryrun | sink | passthrough
  export GOG_SINK_ADDR=injection-sink@본인계정    # sink 모드일 때 사용
  # 그리고 gog 스킬이 'gogcli' 대신 이 스크립트를 부르도록 배선(README 4단계)

이 shim은 gogcli의 세부 문법을 몰라도 동작하도록 방어적으로 작성됨:
  - 모든 argv + stdin 을 통째로 로깅
  - send 판정: argv 안에 send 계열 토큰이 있는지로 판단 (GOG_SEND_TOKENS 로 조정 가능)
  - EXP_ID 추출: argv+stdin 전체에서 정규식 EXP-[A-Za-z0-9_-]+ 스캔
"""
import os, sys, re, json, subprocess, datetime

LOG   = os.path.expanduser(os.environ.get("GOG_TRACE_LOG", "~/.openclaw/trace/gog_trace.jsonl"))
REAL  = os.environ.get("GOG_REAL_BIN", "")              # 진짜 gogcli 경로
MODE  = os.environ.get("GOG_TRACE_MODE", "dryrun").lower()
SINK  = os.environ.get("GOG_SINK_ADDR", "")
SEND_TOKENS = os.environ.get("GOG_SEND_TOKENS", "send,mail-send,send-mail,sendmail").split(",")
EXP_RE = re.compile(r"EXP-[A-Za-z0-9_-]+")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log_event(stage, **fields):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rec = {"timestamp": now(), "source": "gog_shim", "stage": stage}
    rec.update(fields)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def find_exp_id(blob):
    m = EXP_RE.search(blob or "")
    return m.group(0) if m else None


def is_send(argv):
    low = [a.lower() for a in argv]
    return any(tok.strip() and tok.strip() in low for tok in SEND_TOKENS) \
        or any("send" in a for a in low)


def main():
    argv = sys.argv[1:]
    stdin_data = ""
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = ""
    blob = " ".join(argv) + " " + stdin_data
    exp_id = find_exp_id(blob)
    sending = is_send(argv)

    if not sending:
        # ---- READ 계열: 통과 + 반환 로깅 (S1) ----
        if not REAL:
            log_event("S1_read", exp_id=exp_id, argv=argv, note="GOG_REAL_BIN 미설정 - 통과 불가")
            print("ERROR: GOG_REAL_BIN not set", file=sys.stderr)
            return 2
        proc = subprocess.run([REAL] + argv, input=stdin_data,
                              capture_output=True, text=True)
        log_event("S1_read", exp_id=find_exp_id(blob + " " + proc.stdout),
                  argv=argv, returncode=proc.returncode,
                  stdout=proc.stdout[:20000], stderr=proc.stderr[:4000])
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    # ---- SEND 계열 ----
    # S5: 발송 호출이 라우팅되어 shim까지 도달했다는 사실 기록
    log_event("S5_send_routed", exp_id=exp_id, argv=argv,
              stdin=stdin_data[:8000], mode=MODE)

    if MODE == "dryrun":
        # 실제 발송 차단. "여기까지 왔다"만 기록.
        log_event("S7_send_blocked", exp_id=exp_id, argv=argv,
                  note="dryrun: 실제 발송 차단됨")
        print("DRYRUN: send intercepted, not delivered", file=sys.stderr)
        return 0

    if MODE == "sink":
        if not (REAL and SINK):
            log_event("S7_send_error", exp_id=exp_id,
                      note="sink 모드인데 GOG_REAL_BIN/GOG_SINK_ADDR 미설정")
            print("ERROR: sink mode needs GOG_REAL_BIN and GOG_SINK_ADDR", file=sys.stderr)
            return 2
        # 수신자로 보이는 인자를 sink로 강제 치환 (이메일 패턴)
        new_argv = [re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", SINK, a) for a in argv]
        new_stdin = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", SINK, stdin_data)
        proc = subprocess.run([REAL] + new_argv, input=new_stdin,
                              capture_output=True, text=True)
        log_event("S7_send_redirected", exp_id=exp_id, sink=SINK,
                  orig_argv=argv, new_argv=new_argv, returncode=proc.returncode,
                  stdout=proc.stdout[:8000], stderr=proc.stderr[:4000])
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode

    # passthrough
    if not REAL:
        log_event("S7_send_error", exp_id=exp_id, note="passthrough인데 GOG_REAL_BIN 미설정")
        return 2
    proc = subprocess.run([REAL] + argv, input=stdin_data,
                          capture_output=True, text=True)
    log_event("S7_send_exec", exp_id=exp_id, argv=argv, returncode=proc.returncode,
              stdout=proc.stdout[:8000], stderr=proc.stderr[:4000])
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
