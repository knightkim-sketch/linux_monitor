# Linux fleet workload monitor

윈도우 PC에서 여러 회사 리눅스 머신의 CPU / 메모리 부하를 브라우저로 한눈에 보는 도구입니다.
리눅스 머신에는 **아무것도 설치하지 않습니다** — 이미 떠 있는 sshd만 사용합니다.

## 동작 방식

윈도우에서 `monitor.py` 하나를 실행하면, 파이썬이 주기적으로 각 리눅스에 SSH로 붙어
`/proc/stat`·`/proc/meminfo`를 읽어 CPU/메모리 사용률을 계산하고, 로컬 웹서버(포트 8000)로
대시보드를 띄웁니다. 브라우저에서 http://localhost:8000 에 접속하면 됩니다.

- 여유 있는 머신이 위로 오는 정렬 (작업 던질 곳을 바로 찾기)
- 상태색: 초록=여유(<50%), 주황=바쁨(50–85%), 빨강=포화(>85%)
- CPU/메모리 미니 그래프 (최근 60 샘플)
- 접속 실패 머신은 offline 카드로 에러와 함께 표시

## 설치 (윈도우 기준)

1. 파이썬 3.8+ 설치 (https://www.python.org, 설치 시 "Add to PATH" 체크)
2. 명령 프롬프트에서:

   ```
   pip install paramiko
   ```

3. `hosts.example.json` 을 `hosts.json` 으로 복사한 뒤 본인 머신 목록으로 수정.

   - `key_file`: SSH 키를 쓰는 경우 (권장). 예: `~/.ssh/id_rsa`
   - `password`: 비밀번호 인증을 쓰는 경우. (평문 저장이니 키 방식 권장)
   - 둘 다 지정하면 키를 먼저 시도합니다.

4. 실행:

   ```
   python monitor.py
   ```

5. 브라우저에서 http://localhost:8000 접속.

## 설정값 (monitor.py 상단)

- `WEB_PORT` — 대시보드 포트 (기본 8000)
- `POLL_INTERVAL` — 폴링 주기 초 (기본 5)
- `SSH_TIMEOUT` — 접속 타임아웃 초 (기본 8)
- `HISTORY_LEN` — 그래프에 유지할 샘플 수 (기본 60)

## 팁

- SSH 키를 미리 각 머신에 등록해두면(`ssh-copy-id`) 비밀번호 없이 조용히 폴링됩니다.
- 같은 네트워크의 다른 동료도 보게 하려면, 방화벽에서 8000 포트를 열고
  `http://<윈도우PC_IP>:8000` 으로 접속하면 됩니다. (서버는 0.0.0.0 바인딩)
- 머신이 20대를 넘어가면 `POLL_INTERVAL` 을 늘려 부하를 낮추세요.
- EDA 워크로드(Vivado/QuestaSim)가 메모리를 크게 먹는 머신은 memory 막대가
  빨강으로 먼저 뜨므로, 스와핑으로 머신이 멈추기 전에 미리 확인할 수 있습니다.

## Sample View
<img width="1860" height="848" alt="image" src="https://github.com/user-attachments/assets/f39d38e5-d123-454c-aa7f-7c829d52cee3" />

