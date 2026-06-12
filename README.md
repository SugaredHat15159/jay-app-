# jay-pc-agent

Windows/Linux tray agent that connects JAY to your PC over the Tailscale network.

## What it does

- **Room → PC**: Say "Hey Jarvis, open YouTube on PC" → opens in your browser
- **Tray toggle**: Enable/disable command execution from the system tray
- **Extensible**: Add media keys, app launch, type-text actions in `agent.py`

## Setup

```
pip install -r requirements.txt
copy config.ini.example config.ini
# Edit config.ini — add your broker password
python agent.py
```

## MQTT topics

| Topic | Direction | Payload |
|---|---|---|
| `pc/command` | broker → agent | `{"action": "open_url", "url": "..."}` |
| `pc/command` | broker → agent | `{"action": "open_app", "app": "notepad"}` |
| `pc/command` | broker → agent | `{"action": "ping"}` |
| `pc/status`  | agent → broker | `{"client_id": "...", "status": "..."}` |

## Triggering from JAY (room voice)

The NLP `extract_target_device` already recognises "on PC" / "on my computer".
The server-side `pc-router` skill (coming next) will forward matching commands
to `pc/command`. For now you can test directly:

```bash
# From the jay server
mosquitto_pub -h 127.0.0.1 -p 1883 -t pc/command \
  -m '{"action":"open_url","url":"https://youtube.com"}'
```
