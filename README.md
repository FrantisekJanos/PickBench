# PickBench

A playful benchmark for local **decision models** — small models that do not write text but pick one option from a fixed list. PickBench talks to them through the Ollama `/v1/systemone` endpoint and shows the result as a game: a car on a multi-lane road that goes where the model tells it to.

It measures how fast the model decides (latency percentiles, decisions per second, energy per decision on an NVIDIA GPU) and how well (agreement with an ideal driver, calibration of the reported confidence, stability across rephrased prompts, resistance to an instruction hidden in the input).

The game page and the command-line tools work in Czech and English. The prompts sent to the model are in English either way, so the interface language does not change a request.

## Requirements

- Python 3 (standard library only, nothing to install)
- [Ollama](https://ollama.com) 0.35 or newer with at least one decision model pulled
- optional: an NVIDIA GPU with `nvidia-smi` for temperature, power and energy readings — without it everything else still works

## Run

```
python server.py
```

Then open http://127.0.0.1:8741. The server listens on `127.0.0.1` only, refuses requests that come from other web pages and does not let them show the page in a frame. It has no login: any program or user on the same computer can call it, and with provider keys set that includes sending hosted requests up to the spending cap. Do not expose the port to a network.

| Variable | Default | Meaning |
|---|---|---|
| `PICKBENCH_PORT` | `8741` | port of the game server |
| `PICKBENCH_OLLAMA` | `http://127.0.0.1:11434` | address of Ollama |
| `PICKBENCH_DB` | `pickbench.db` next to `server.py` | SQLite file with results, last settings and the scheduler queue |
| `PICKBENCH_LANG` | `cs` | language of the command-line tools and of the lines the server prints at start: `cs` or `en` |

Tests: `python -m unittest discover -s tests` (no GPU, Ollama or network needed).

## Languages

The page has a **CS / EN** switch in its header. A click changes the texts in place and keeps the settings, the queue and the results. The browser remembers the choice. On a first visit the page follows the browser language: Czech for Czech and Slovak, English for any other. `?lang=en` or `?lang=cs` in the address overrides both, and the browser remembers that too. You cannot switch the language while a game, a benchmark or the queue is running.

`bench.py`, `hwbench.py` and `fetch_datasets.py` take `--lang cs|en` anywhere among their arguments. Without the option the environment variable `PICKBENCH_LANG` decides, and without the variable the output is Czech. A tool given an unknown language prints a message and exits with a non-zero code.

The page sends its language with every request in the `X-PickBench-Lang` header, and the server writes its error messages in that language; a request without the header gets Czech. `PICKBENCH_LANG` changes only the lines `server.py` prints at start.

Three kinds of text keep the language they came in:

- the content of a dataset: its name, the e-mails, the labels of a public dataset (category names of an e-mail dataset have an optional English form, `label_en`);
- messages from Ollama, from a provider and from the operating system;
- text stored earlier, such as the error message of a queue step.

Numbers are the same in both languages, with a decimal point. On the page two formats differ: a percentage has a space in Czech (`85 %`) and none in English (`85%`), and the time of a result follows the language. The command-line tools print a percentage without the space in both languages. The one exception is the column of `hwbench.py server` with the share of requests answered in time: `100 %` in Czech output, `100%` in English. The CSV export carries the results table as the page shows it, so its header follows the page language; the page names the file `pickbench-vysledky.csv` in Czech and `pickbench-results.csv` in English.

## Scenarios

- **Obstacles** — every row blocks some lanes; the model gets the distance to the nearest obstacle in each lane and picks the lane with the longest free road. Two to eight lanes.
- **Number duels** — every lane has a gate with a number; the model picks the largest. Results are split by how close the two largest numbers are.
- **E-mails** — the model sorts e-mails from a dataset into categories; the road has one lane per category. A confusion table shows which categories get mixed up.
- **Public datasets** — classification datasets with more labels than the road has lanes, or with a separate list of labels for every task. They run only as a benchmark without the game, because the road has at most eight lanes. The endpoint limits how many options one question may have; see "Public datasets" below.

Drivers: Model, Rules (the ideal driver, the reference for "correct") and Random.

## Run modes

- **Real time** — the car moves at a set speed; a slow answer means a crash or a missed row.
- **Turn-based** — the game waits for every decision; measures quality without the effect of speed.
- **Ladder** — real time with the speed going up level by level until the model makes more than one error per level.
- **Benchmark without the game** — a fixed number of tasks one after another.

Options: a second question per request, *mirrors* (each task sent three ways to measure stability), *Trojan horse* (a quarter of the tasks carry a hidden instruction pointing at a wrong answer).

A run can be limited to a number of road cells, and a **seed** makes every model get the same rows in the same order.

## Scheduler and stored results

The **scheduler** panel holds a queue of steps. Each step remembers the complete settings it was added with (model, scenario, mode, length, seed, options). The queue runs unattended: before each step it unloads the previous step's model from memory when the next step uses a different one, waits a set time and, if asked, until the GPU cools below a set temperature.

Only one browser tab runs the queue at a time. Results, the last settings and the queue are stored in an SQLite file and survive a page reload. If the file cannot be used, the page keeps working without saving and says so under the results table. Settings or a queue that could not be read are not written back, so the stored ones are not overwritten; reloading the page tries again. A result that could not be saved stays on the page only and is counted in that note until you delete the results or reload the page — export it to CSV first. The buttons that start a run or change the queue stay disabled until the page has loaded its stored state.

Every tile, column header and control has a tooltip explaining what the number means and what it is computed from.

## Command line

- `bench.py` — compares models on the same set of tasks: `python bench.py 100 model-a model-b`. Options: `--lanes K`, `--numbers`, `--emails datasets/cz-firemni-emaily.json`, `--mirror`, `--inject`.
- `hwbench.py` — measures how this machine serves one model: `python hwbench.py latency --n 200 model-a`. Tests: `latency`, `cold`, `concurrency`, `questions`, `context`, `soak`, `server`.
- `fetch_datasets.py` — downloads the public datasets: `python fetch_datasets.py banking77 clinc150 massive`.

All three print Czech by default and English with `--lang en`; see "Languages" above.

`bench.py --public datasets/public/massive-en-us.json model-a` measures a public dataset and prints accuracy, macro-F1 (the mean of F1 over the classes), the worst classes and the most frequent confusions; for a dataset with an out-of-scope label also how many out-of-scope tasks the model recognized. `--deadline-ms L` adds **goodput**: the share of decisions that were both correct and in time.

The serving tests follow the MLPerf Inference scenarios: `latency` is SingleStream and reports the 90th percentile, `server` sends requests at random (Poisson) arrival times and looks for the highest rate at which the 99th percentile of latency, counted from the planned arrival, stays under a limit (`--p99-ms`), and `concurrency` is Offline. These are MLPerf-style measurements, not an MLPerf submission under the MLCommons rules.

Do not run the command-line tools while the page is sending requests to the model.

Other files: `index.html` (the whole game and user interface), `server.py` (static server and proxy to Ollama), `store.py` (SQLite storage), `gpu.py` (reads `nvidia-smi`), `metrics.py` (classification metrics).

## E-mail datasets

A dataset is a JSON file in `datasets/`; a new file shows up in the scenario list after a page reload.

```json
{
  "name": "Shown in the scenario list",
  "about": "Optional note for the viewer; the model never sees it.",
  "about_en": "Optional English version of that note.",
  "instructions": "Optional instruction for the model.",
  "categories": [
    {"key": "invoice", "label": "Faktura", "label_en": "Invoice", "description": "What belongs here, for the model.", "letter": "F", "letter_en": "I", "color": "#56b4e9"}
  ],
  "emails": [
    {"subject": "…", "body": "…", "category": "invoice"}
  ]
}
```

Two to eight categories; `key`, `label`, `description`, `subject`, `body` and `category` are required, the rest is optional. The file name may contain only ASCII letters, digits, `_` and `-`. The model sees the subject, the body and the category keys with their descriptions — not the labels, letters or colors.

The English page shows `about_en` in place of `about`, and `label_en` and `letter_en` in place of a category's `label` and `letter`; a dataset without them shows `about`, `label` and `letter` in both languages. The model never receives the category names (it receives `key` and `description`), so translating them does not change a measurement. The page does not translate anything else in the file, so write `name` in the language your viewers read.

The datasets written for this project (`datasets/cz-firemni-emaily.json` and others) are in Czech. The shipped sample of CFPB complaints is in English; see "Public datasets" below.

## Public datasets

`fetch_datasets.py` downloads the datasets with the Python standard library only and converts them to a JSON format (`source`, `license`, `labels` or per-task `options`, `items` with `text` and `label`). By default it keeps a stratified sample of 500 tasks with a fixed seed; `--full` keeps the whole test split. Each file carries the note for the viewer in both languages (`about` and `about_en`), whichever language the tool runs in; the dataset name it writes ends with a Czech note on the sample. The files go to `datasets/public/`, which is not part of this repository — the data belong to their authors and come under their own licenses. The one exception is `cfpb`: it writes `datasets/cfpb-complaints-<count>.json`, so with the default of 50 complaints per product (also as part of `all`) it replaces the shipped sample described below.

The datasets:

| Dataset | Task | License | Authors |
|---|---|---|---|
| MASSIVE 1.1 (en-US) | voice assistant request → one of 18 scenarios | CC BY 4.0 | Amazon — FitzGerald et al., "MASSIVE: A 1M-Example Multilingual Natural Language Understanding Dataset with 51 Typologically-Diverse Languages", 2022, https://github.com/alexa/massive; the English utterances come from SLURP (Bastianelli et al., 2020, CC BY 4.0) |
| BANKING77 | banking query → one of 77 intents | CC-BY-4.0 | PolyAI — Casanueva et al., "Efficient Intent Detection with Dual Sentence Encoders", 2020, https://github.com/PolyAI-LDN/task-specific-datasets |
| CLINC150 | request → one of 150 intents or out of scope | CC-BY-3.0 | Larson et al., "An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction", 2019, https://github.com/clinc/oos-eval |
| Horizon-Labs multilingual-zeroshot-synthetic (Czech rows) | Czech customer message → one of 2 to 10 offered labels | ODC-BY 1.0 | Horizon-Labs, https://huggingface.co/datasets/Horizon-Labs/multilingual-zeroshot-synthetic — texts and labels were written by a language model and not checked by a person; web passages in the full dataset come from FineWeb |
| CFPB Consumer Complaint Database | complaint → one of 6 financial products | CC0 | U.S. Consumer Financial Protection Bureau, https://www.consumerfinance.gov/data-research/consumer-complaints/ |

A sample of 300 CFPB complaints is shipped as an ordinary e-mail dataset, `datasets/cfpb-complaints-300.json`. The complaint texts were taken from a third-party copy of the database published on Hugging Face (`BEE-spoke-data/consumer-finance-complaints`, pulled from the bureau in February 2024, CC0 according to its card), because the bureau's own interface does not return complaint texts today.

A label that has no description of its own is sent in the request only once, as the option key; the key is not repeated as a description.

The number of options in one question is limited by the endpoint, not by PickBench. Ollama 0.35.1 refused the questions of BANKING77 and CLINC150 with the message `criteria must contain 2–26 candidates`, so these two datasets can be downloaded and checked but not measured through this endpoint as they are. `bench.py` then reports that the dataset did not fit and does not measure it; the list of labels is never shortened silently.

On the page the downloaded files appear in the scenario list after a page reload. The results table has the columns macro-F1 and goodput.

## Hosted models

Besides local Ollama models, PickBench can call two hosted decision models through `providers.py`: **Jev** by TypeSafe AI (`jev:jev-1.13.0`) and **Clef** on Cloudflare Workers AI (`cf:clef-flash`, `cf:clef`). They are offered only when the process has the provider's keys in its environment; without keys nothing changes.

| Variable | Meaning |
|---|---|
| `TYPESAFE_API_KEY` | key for Jev |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | account and a Workers AI token for Clef |
| `PICKBENCH_MAX_USD` | spend cap in dollars per server run (default 1) |
| `PICKBENCH_MAX_HOSTED` | cap on the number of hosted requests per server run (default 5000) |

```
python bench.py 100 jev:jev-1.13.0 cf:clef-flash
python hwbench.py latency --n 200 cf:clef
```

- Keys are read from the environment only and are never sent to the browser, written to the database, printed or included in an error message.
- Hosted requests cost money and the task texts leave this computer: the tasks of any dataset you select go to the provider with their full text, whether the dataset is bundled, downloaded or your own. The only safeguard is the confirmation the page asks for the first time a provider is used; `bench.py` and `hwbench.py` ask nothing and send right away.
- When the cap is reached, no further hosted requests are sent and the run ends with a message. `bench.py` and `hwbench.py` take `--max-usd` and `--max-hosted`. Restarting resets the counter, so keep the balance at the provider small.
- The latency of a hosted model includes the network. Results show the price per 1000 decisions instead of energy, plus an estimate of the connection time.
- `hwbench.py` skips `cold`, `concurrency` and `server` for hosted models and runs `soak` only with `--hosted-soak`.

## License

MIT — see `LICENSE`. The license covers the code and the Czech e-mail datasets written for this project; the public datasets above keep their own licenses. The bundled sample `datasets/cfpb-complaints-300.json` is not covered by MIT: its `source` and `license` fields say where the texts come from.

Ollama, Jev, TypeSafe, Clef, Cloudflare and the model names are trademarks of their owners. PickBench is an independent project and is not affiliated with, sponsored by or endorsed by any of them.

To report a security problem, see `SECURITY.md`.
