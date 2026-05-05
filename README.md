# Tactical Systems HUD

A low-overhead terminal HUD aimed at stress-testing constrained systems (including Termux/Android-like environments) while minimizing observer effect.

## Features mapped to requirements

- **Absolute minimal overhead**
  - Single-process, single-threaded `curses` loop.
  - Non-blocking telemetry/log reads.
  - Configurable refresh interval (default `1.0s`).
  - No heavyweight UI framework.

- **Granular RAM tracking**
  - Active RAM (`used`).
  - Cached + buffers (`cached + buffers`).
  - Available RAM (`available`).
  - Swap usage (`swap used/total`).

- **Predictive thermal warnings**
  - Reads available temperatures via `psutil.sensors_temperatures()`.
  - Computes temperature slope from sample history.
  - Projects temperature into the future (`--thermal-horizon`).
  - Escalates to `WARNING` or `CRITICAL` if projection crosses thresholds.

- **Process-targeted telemetry + live tailing**
  - Track a specific process via `--pid` or fuzzy match via `--match`.
  - Shows target process CPU, RSS, status.
  - Tail log file with `--tail-file` and/or command output with `--tail-cmd`.

- **Graceful degradation**
  - Wraps telemetry update cycle in fail-safe exception handling.
  - On transient read failure/freezes, displays recovery message, waits briefly, and resumes.

## Install

```bash
python3 -m pip install psutil
```

## Run

```bash
python3 hud.py
```

Target a process and tail a file:

```bash
python3 hud.py --match "python quantize_script.py" --tail-file /path/to/log.txt
```

Target a PID and stream command output:

```bash
python3 hud.py --pid 12345 --tail-cmd "tail -F /path/to/log.txt"
```

Press `q` to quit.
