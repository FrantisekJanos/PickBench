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
- **Bans and exceptions** — every row has its own vehicle and every lane a traffic sign, such as `buses only`, `no trucks` or `vehicles up to 3.5 t only`; the model picks the one lane the vehicle may enter. The road shows each sign as a symbol and the vehicle of the nearest row under the car; the full texts are in the last decision panel. Results are split by the class of the sign of the open lane. Pairs of tasks (`--pairs`) are only in `bench.py`.
- **Memory trace** — every row of gates is its own task with its own history of signs passed; every gate shows a sign and the model picks the gate with the sign passed a given number of steps back. The state carries the history with a label for every step back, so the scenario measures looking an item up, not memory. A strip under the road shows the history of the nearest row, the most recent sign on the right. Results are split by the number of steps back, with the error line and the memory span below. The `--count` form is only in `bench.py`.
- **Barrier** — every row is an intersection with a barrier, a traffic light and a sign, and depending on the task a police officer, an emergency vehicle or a pedestrian on the crossing. The model answers a `noul` question with the probability that the car may drive through; the car goes at 0.5 or more. Before the row the page shows the decision (GO or STOP with the value). On a stop that arrives in time the car stands at the barrier for 0.8 s; that time does not count towards decisions per second. Going where it should have stopped is a crash, and without an answer in time the car goes. After the row a label names the result in words (drove through, stopped, should have stopped, stopped needlessly, invalid answer, missed) with the deciding rule, and a legend under the road explains the pictograms. Results are split by the deciding rule and by the class of the task, with false go, needless stop, invalid answers, AUROC and the best threshold below.
- **Two steps ahead** — every row is a block of 4 rows with gaps between them and obstacles across the road, and the car starts in the lane given by the task; the blocks arrive every 6 cells. The model picks one move for the nearest row (one lane left, stay or one lane right) that lets the car get through all 4 rows; in part of the tasks the move of a greedy driver, towards the longest free run straight ahead, leads into a dead end. An arrow before the block shows the chosen move, and the car follows it, changing lanes in the gaps between the rows. After the car passes, a green line shows the right path and, after a wrong move, an orange line the path of the greedy driver. A crash or a dead end gets a red cross at the cube, and the car drives through the rest of the block see-through; a label under the car names the result (right move, crash in row N, off the road, dead end in row N, invalid answer, missed). Results are split by trap depth, with the error line of `bench.py` below. Three to eight lanes; the `--rows` form is only in `bench.py`.
- **Hidden rule** — every row has three gates, and every gate shows only the feature it shares with the sample (color, shape or count). A hidden rule says which feature is right, and it changes after every three correct picks in a row; every row carries its own log of earlier picks, shown in a strip under the road with a line where the rule changed. Under each gate the page writes what it matches ("match: color"), and an outline marks the picked gate before the car passes. A gate the log rules out is crossed out: the old rule after a change ("old rule, no longer holds") and a feature that got a wrong pick since the last change, so only the right gate is left uncrossed in the switch and eliminate classes. After the car passes, the right gate turns green and a label under the row names the result, including perseveration (in the switch class, a pick of the old rule's gate). Results are split by class, with short and long logs and perseveration below. The `--matched-by` form is only in `bench.py`.
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

- `bench.py` — compares models on the same set of tasks: `python bench.py 100 model-a model-b`. Options: `--lanes K`, `--numbers`, `--signs`, `--pairs`, `--memory`, `--count`, `--barrier`, `--lookahead`, `--rows`, `--blocked`, `--rule`, `--labeled`, `--matched-by`, `--queue`, `--weighted`, `--bet`, `--cruise`, `--timetable`, `--sorted`, `--shuffled`, `--emails datasets/cz-firemni-emaily.json`, `--mirror`, `--inject`.
- `hwbench.py` — measures how this machine serves one model: `python hwbench.py latency --n 200 model-a`. Tests: `latency`, `cold`, `concurrency`, `questions`, `context`, `soak`, `server`.
- `fetch_datasets.py` — downloads the public datasets: `python fetch_datasets.py banking77 clinc150 massive`.

All three print Czech by default and English with `--lang en`; see "Languages" above.

`bench.py --signs` gives every lane a traffic sign, such as `buses only`, `no vehicles over 3.5 t` or `all vehicles except trucks`, and asks the model for the one lane the vehicle may enter; the note after the model row splits the accuracy by sign class. `--pairs` adds a twin of every task in which only the sign of the open lane is worded the other way (`no trucks` becomes `all vehicles except trucks`), so the answer stays the same, and reports how often the model got both tasks of a pair right.

`bench.py --memory` lets the car pass a series of roadside signs and then offers a row of gates, each showing one sign; the model picks the gate with the sign passed a given number of steps back (1 to 8). A decision model has no memory of its own, so the state carries the signs passed: by default as a map from a label such as `3 steps back` to the sign, with the label to find in `target`, so the scenario measures looking an item up by its label, not memory. `--count` gives the same tasks with the history as a plain list (`signs_passed`, oldest first) and the number of steps as a number (`steps_back`), so the model has to count the position in the list. The gates show a run of consecutive signs from the history that contains the right one, in shuffled order, so the shape of the offer does not give the answer away. There are 4 gates by default. The note after the model row gives the accuracy by steps back; the line below counts the errors (one step off, most recent sign, other, invalid) and gives the memory span, the largest number of steps back up to which every number of steps is at least 80% correct. With 100 tasks about 12 fall on each number of steps, so the span is a rough estimate. The page has the default form of the scenario, in the game and as a benchmark; for the same seed it gives the same tasks to every model, but other tasks than `bench.py`, so compare the two only statistically.

`bench.py --barrier` brings the car to an intersection and asks whether it may drive through. It is the first scenario with a `noul` question: the model answers with the probability of "yes", and the car goes when it is at least 0.5. The state gives a police officer, an emergency vehicle, a pedestrian on the crossing, the traffic light and the sign; the prompt lists five rules in order of priority, so the scenario measures reading the prompt and the state, not knowing the traffic code. Three quarters of the tasks are "go", and in half of the tasks decided by one of the first four rules the light, or the sign when the light is off, says the opposite (when the light decides, the sign does). The note after the model row gives the accuracy by deciding rule; the line below counts false go (went when it should have stopped), needless stop and invalid answers, splits the accuracy into four classes (the light, or the sign when the light is off, agrees with the deciding rule; a higher rule overrides the light; the light overrides the sign; last rule), and gives AUROC and the best threshold with its balanced accuracy, which shows a model that ranks the tasks well but sits on the wrong side of 0.5. With `--mirror` the second variant is the inverted question "Must the car stop now?". It cannot be combined with `--lanes`. The page has this scenario too, in the game and as a benchmark; for the same seed it gives the same tasks to every model, but other tasks than `bench.py`, so compare the two only statistically.

`bench.py --lookahead` puts 4 rows with obstacles ahead of the car. Before each row the car may shift one lane left, stay or shift one lane right, and then it enters the row; the model picks the move for the nearest row that lets the car pass the most rows. Every task has exactly one right move, and with it the car passes all the rows. The tasks are spread evenly over three classes of trap depth, the number of nearest rows the model has to weigh to get the move right (1, 2, 3 or more); at depth 1 it is enough not to crash right away. At depths 2 and 3 or more the generator keeps the move of a greedy driver from giving the answer more often than chance; with 4 lanes or more it does the same for the move towards the lane with the most free cells around it, and with 5 lanes or more for the move towards the middle of the road. With 3 lanes these two moves are right in most tasks. The note after the model row gives the accuracy by trap depth; the line below counts the errors: the move of a greedy driver, who takes the lane with the longest free run straight ahead, a crash or a move off the road, other and invalid. There are 4 lanes by default (`--lanes` 3 to 8). The state has two forms: `--rows` gives every row as a string with one character per lane, `--blocked` lists the numbers of the blocked lanes in every row; without the option the `blocked` form applies. The page has this scenario too, with the blocked form only; for the same seed it gives the same tasks to every model, but other tasks than `bench.py`, so compare the two only statistically.

`bench.py --rule` is built on a sample card and three gates; every card has a color, a shape and a count, and every gate matches the sample in exactly one of them. A hidden rule says which feature makes a gate right, and it changes to another feature right after every three correct picks in a row. The state carries a log of earlier picks, and the model picks the gate by the rule in force now. By default (`--labeled`) the model sees no cards: every gate says only which feature (color, shape or count) it shares with the sample, and the log names for every pick the feature the picked gate shared with the sample. `--matched-by` gives the sample card and the gate cards as text, so the model has to find the shared feature itself, with the same log. The tasks are spread evenly over three classes, and within every class evenly over short and long logs: repeat (a correct pick since the last change gives the rule), switch (the rule has changed, and ruling out the old feature and a wrong pick leaves the new one) and eliminate (no change yet, two different wrong picks leave the third feature). In the switch class the generator keeps the least-tried feature from giving the answer more often than chance, and in long logs also the feature of the first pick; in short switch logs the first pick is often the answer. In the eliminate class the least-tried feature is always the answer, because that is the class. The note after the model row gives the accuracy by class; the line below gives the accuracy for short (3 to 5 picks) and long (6 to 10 picks) logs and the perseveration, how often the model picked the gate of the old rule in the switch class. It cannot be combined with `--lanes`. The page has this scenario too, with the labeled form only; for the same seed it gives the same tasks to every model, but other tasks than `bench.py`, so compare the two only statistically.

`bench.py --queue` puts the car at a toll gate with 4 lanes by default (`--lanes` 2 to 8); every lane holds a queue of 1 to 12 vehicles, and the model picks the lane with the shortest queue. The shortest queue is always unique, and the tasks are spread evenly over three classes of the difference between the shortest and the second shortest queue (1, 2 to 3, 4 or more). A model can also guess the lane whose list of vehicles is the shortest as text; at differences 1 and 2 to 3 the generator makes that guess right in a third of the tasks (in half of them with 2 lanes), at 4 or more it is right almost always. With `--weighted` a truck, a bus and a tractor count as three vehicles; in half of the tasks the queue with the fewest vehicles is another lane, so counting without weights fails there. With 3 lanes or more the generator also keeps the lane with the fewest heavy vehicles right in a third of the tasks at differences 1 and 2 to 3; with 2 lanes, and at 4 or more, that lane is right in most tasks. The weighted tasks differ from the unweighted ones. The note after the model row gives the accuracy by difference; the line below counts the errors: the shortest queue by count (with `--weighted` only), the shortest text, other and invalid. The page does not have this scenario.

`bench.py --bet` shows 4 gates by default (`--lanes` 3 to 8); every gate pays its reward with a given chance in percent, and the model picks the gate with the highest expected payout, the reward times the chance, at least 5% higher than at any other gate. The tasks are spread evenly over three levels: the same chance at every gate (a plain number duel), chances of 50 or 100% (the gate with 100% is wrong, so the gate with the highest reward is always right) and general chances from 10 to 95%. In the general level the generator keeps the gate with the highest reward right in a third of the tasks, and the same for the gate with the highest chance; with 4 gates or more also for the gate with the second highest reward. With 3 gates ruling out the gates with the highest reward and the highest chance still gives the answer in almost half of the general tasks. The note after the model row gives the accuracy by level; the line below gives the regret, the mean loss of expected payout against the best gate (an invalid answer counts as a full loss), and counts the errors: the gate with the highest reward, the gate with the highest chance, other and invalid. The page does not have this scenario.

`bench.py --cruise` asks for a cruise control speed level from 1 (slowest) to 7 (fastest). The prompt holds the table: the distance to the obstacle ahead (8 to 300 m) sets the starting level in seven bands, a wet road takes one level off and an icy road two, light fog one and dense fog two. The tasks are spread evenly over four classes: table only (dry road, no fog), road, fog and both; in every class half of the distances lie within 3 m of a band boundary. The generator weighs the combinations so that cheap shortcuts, a fixed level, a guess by the number of digits of the distance or the conditions alone, get at most about a third of the road, fog and both tasks right. Reading the table roughly (about one level per 40 m) still gets about half of them right, and a fixed level is within one level of the right one in about three quarters of them, so accuracy and MAE are the main numbers and the share within one level is an extra. The note after the model row gives the accuracy by class; the lines below give the accuracy near a boundary and inside a band, the share within one level, MAE (an invalid answer counts as 6), the counts of answers that are too fast and too slow, and the error kinds: distance only, a condition left out, each condition as one level, the neighboring band, other and invalid. The page does not have this scenario.

`bench.py --timetable` routes a parcel by a timetable: the state holds the parcel's ZIP code (10000 to 99999) and service, a table `routes` of 3 to 12 rows, each sending one range of ZIP codes (from `zip_from` to `zip_to`, both ends included) to a depot, and an optional exception that sends one service to its own depot first. The model picks one of six depots. The tasks are spread evenly over three table sizes (3 to 5, 6 to 8, 9 to 12 rows), three positions of the ZIP code (well inside a row, a few codes from a boundary, exactly on a boundary) and three exception cases (none, one that applies, one for another service). Close to a boundary the answer is always one of the two neighboring rows, so chance is one half there, not one sixth; a ZIP code exactly on a boundary stands in the table as it is, so that class tests whether the model takes both ends as included. An exception applies in half of the tasks that have one. By default the rows are sorted by ZIP code; `--shuffled` gives the same tasks with the rows in random order (`--sorted` names the default). The note after the model row gives the accuracy by table size; the lines below give the accuracy by position (only tasks without an exception that applies) and by exception case, and count the errors: neighboring row, exception missed, exception wrongly applied, other and invalid. The page does not have this scenario.

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
