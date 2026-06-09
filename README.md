# smowl-analyser

Python CLI for extracting Blackboard + SMOW report data into local, auditable JSON output.

## V1 scope

This first version extracts and stores data, then can generate a local triage report for human
review. It does not generate images or make disciplinary conclusions.

The default Blackboard URL is `https://insper.blackboard.com`. Pass `--url` only when you need
to override it.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

## Recommended flow

```bash
smowl-analyser doctor
smowl-analyser login
smowl-analyser extract
smowl-analyser analyze --run-id "<run_id>"
```

The recommended extraction path is now manual navigation until the SMOW student list. Use
`extract`, navigate yourself to the page where the students are listed, press Enter in the
terminal, and let the tool download the available structured data and evidence files from there.

Use `auto-extract` only for the older semi-automatic crawler flow. Use `learn` when the crawler
needs to be recalibrated against the real Blackboard/SMOW page structure. Use `resume` after an
extraction stops in the middle. Use `analyze` after extraction to create a local HTML triage
report.

If `extract` says it did not capture SMOW internal data, run `smowl-analyser diagnose`, navigate
to the same page, and inspect `data/diagnostics/{session_id}/summary.json`.

## Commands

### `doctor`

Checks whether the local environment is ready.

```bash
smowl-analyser doctor
```

It reports:

- whether the Playwright session file exists;
- whether required Python packages are importable;
- where extraction runs will be stored.

This command does not open the browser and does not access Blackboard.

### `login`

Opens a visible Chromium browser so you can log in manually through Blackboard/SSO/MFA.

```bash
smowl-analyser login
```

After login, return to the terminal and press Enter. The authenticated browser session is saved
to `.secrets/playwright-state.json`, so later commands can reuse it without storing your password.

Useful options:

```bash
smowl-analyser login --url "https://another.blackboard.host"
smowl-analyser login --slow-mo-ms 250
```

### `learn`

Records the real manual path through Blackboard and SMOW so the crawler can be improved safely.
It saves sanitized HTML, page summaries, frame snapshots, route data, network deltas, JSON/API
responses and download event metadata.

```bash
smowl-analyser learn
```

The command opens the browser and asks you to navigate manually through these checkpoints:

1. Chegue na página da turma e pressione Enter.
2. Abra o SMOW e chegue na lista de alunos monitorados; depois pressione Enter.
3. Clique em um aluno ou em um ícone de detalhe/ação de um aluno e pressione Enter.
4. Abra detalhes/evidências de FrontCamera para esse aluno e pressione Enter.
5. Abra detalhes/evidências de ComputerMonitoring para esse aluno e pressione Enter.
6. Clique no botão de download do aluno ou de relatório individual e pressione Enter.
7. Abra uma evidência/imagem específica, se existir, e pressione Enter.

Output is saved in `data/learning/{session_id}/`.

For each checkpoint, `learn` saves per-checkpoint files in `network/` showing which requests and
downloads appeared after that action. This is the main tool for discovering which SMOW click
triggers evidence/download endpoints.

It does not save screenshots or generate images. HTML, JSON values and URLs are sanitized to
reduce exposure of cookies, tokens, emails and long numeric identifiers.

### `inspect`

Runs a dry inspection of the available course, SMOW activity, report and student list.

```bash
smowl-analyser inspect
```

The command opens Blackboard, moves to the `Cursos` tab, lists real course pages, and then follows
the selected course into SMOW.

Use this before a full extraction to confirm that the crawler is finding the expected course and
SMOW report. It may ask you to choose from options in the terminal. It does not download evidence
files.

### `manual-extract`

Alias for `extract`. It opens Blackboard with your saved session and waits while you navigate
manually to the SMOW page where the report/list of students is visible.

```bash
smowl-analyser manual-extract
```

When the student list is open in the browser, return to the terminal and press Enter. From that
point onward, the tool automatically:

- captures SMOW JSON/API responses that loaded during your navigation;
- parses the current SMOW page or SMOW results frame;
- clicks known per-student SMOW detail controls such as `FrontCamera` and `ComputerMonitoring`
  to trigger evidence JSON endpoints;
- creates `selection.json`, `manifest.json`, `progress.json` and `report.json`;
- creates `students/{student_id}/computer_monitoring.json` for a student;
- immediately downloads that student's ComputerMonitoring screenshots in parallel and links them
  back into the JSON before moving to the next student;
- saves progress student by student, so `resume` can continue later.

This command does not ask you to choose course, activity or report. It uses the current browser
page as the report starting point.

### `extract`

Opens Blackboard with your saved session and waits while you navigate manually to the SMOW page
where the report/list of students is visible.

```bash
smowl-analyser extract
```

When the student list is open in the browser, return to the terminal and press Enter. From that
point onward, the tool automatically captures the current page as the report starting point,
parses students and fetches ComputerMonitoring data for each student. For each student, it saves
`students/{student_id}/computer_monitoring.json`, immediately downloads that student's screenshots
under `files/{student_id}/...` in parallel, and links them back through
`screenshots[].local_path`. This avoids letting short-lived SMOW image URLs expire while later
students are still being processed.

Use `--download-workers` to control parallel image downloads:

```bash
smowl-analyser extract --download-workers 8
```

Use `--download-workers 1` for the older sequential download behavior.

This command does not ask you to choose course, activity or report.

### `diagnose`

Captures a sanitized diagnostic bundle after you navigate manually to the SMOW student list.

```bash
smowl-analyser diagnose
```

The command does not download evidence files and does not save screenshots. It saves:

- `summary.json`: pages, frames, domain counts and SMOW endpoint counts;
- `pages.json` and `frames.json`: URLs, titles and saved artifact paths;
- `network/responses.json`: sanitized response metadata from the whole browser context;
- `network/json_responses.json`: sanitized JSON payloads captured from the whole browser context;
- `pages/*.html` and `frames/*.html`: sanitized HTML snapshots.

Use this when `extract` cannot see `/lti/ajax/students` or `/V2/results/figures`. The important
signals are `has_smowlresults_frame`, `has_front_results_frame`, `lti_ajax_students`,
`results_figures` and `monitoring_evidence`.

### `auto-extract`

Runs the older semi-automatic crawler flow.

```bash
smowl-analyser auto-extract
```

The command opens Blackboard, moves to the `Cursos` tab, asks you to choose a course, opens the
selected SMOW activity, and then tries to discover a report automatically.

The extractor now prefers SMOW JSON/API responses captured while the page loads. If the report data
has not loaded yet, it will ask you to navigate to the report/list of students and press Enter.
It accepts the same `--download-workers` option as `extract`.

Each run is stored in `data/runs/{run_id}/` with files like:

- `report.json`: compact run index with course/activity/student summary data. It intentionally
  omits `students[].files` and `students[].smow.computer_monitoring`, because those details live
  in the per-student ComputerMonitoring files;
- `manifest.json`: run metadata, URLs and counts;
- `selection.json`: selected course, SMOW activity and report;
- `progress.json`: pending, completed and failed students;
- `students/{student_id}/computer_monitoring.json`: ComputerMonitoring events for one student,
  including type, timestamp, incident flag, detail payload and linked screenshots;
- `files/{student_id}/...`: downloaded evidence files/images when available.

### `resume`

Continues an extraction that stopped before all students were processed.

```bash
smowl-analyser resume --run-id "20260602-000000"
```

The `run_id` is the folder name under `data/runs/`. The command reads the existing
`selection.json`, `report.json` and `progress.json`, then retries pending or failed students.
It also accepts `--download-workers` for the image download phase.

### `analyze`

Generates a local HTML triage report from an already extracted run.

```bash
smowl-analyser analyze --run-id "20260602-000000"
```

The command reads local `computer_monitoring.json` files and does not access Blackboard, SMOW or
the internet. It saves:

- `analysis/report.html`: human-readable report for review;
- `analysis/summary.json`: structured findings for future automation.

The initial rules flag:

- ComputerMonitoring closed and later launched again;
- programs opened by at most 10% of the class, excluding a small built-in allowlist;
- uncommon external navigation window titles, based on `window_title`;
- copy/paste events with two or more non-empty lines, especially when the text looks like code.

Useful option:

```bash
smowl-analyser analyze --run-id "20260602-000000" --rarity-ratio 0.05
```

## Local data

Local session state is stored in `.secrets/playwright-state.json`.
Extraction output is stored in `data/runs/{run_id}/`.
Learning output is stored in `data/learning/{session_id}/`.
Diagnostic output is stored in `data/diagnostics/{session_id}/`.

These directories are ignored by Git because they can contain sensitive academic and proctoring
data.
