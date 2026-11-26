# M5 Sign-Off and Reading List

Everything from the E5.1–E5.7 plans that needs a human decision, plus what to read and in which order. Tick each box when it's decided, and write the decision next to it.

## 1. Decisions to sign off

### Before anything is pushed (blocking)

- [ ] **D1. E5.1 data is too big to push.** `backend/data/adsblol` is 792 MB, `segments-2026-11-10.parquet` is 108 MB (GitHub's hard limit is 100 MB), and there's no LFS. Pick one:
  - a) Re-encode the Parquet files: round to the data's real precision (~5 cm positions) + float32 + zstd. No re-download. *(recommended, tested: 108 MB → 38 MB, 11/11 E5.1 tests pass; this is guide step 5h)*
  - b) Split each day into several files under 50 MB.
  - c) Set up Git LFS.

  Either way, commit `087ebd0` gets rewritten before the first push. It has never been pushed, so that's safe.
  Decision: ______
- [x] **D2. Keep aircraft with an unknown category?** Checked, and **no change is needed.** Unknown categories are only 0.1% of segments (5 of 4,277 on the largest day). The 4,906 flight hours are real traffic: 91% are A1 light aircraft. Joshua should still know that E5.3 will run longer than the guide says (see D4).
- [ ] **D3. Branch name and PR scope.** The guide uses one branch, `m5-real-data-and-models`. The work is on `e5.1-real-data`. Pick one: keep it, rename it, or open one PR per epic.
  Decision: ______

### Changes from the guide (approve or reject)

- [ ] **D4. Cap the real training samples in E5.3** (new `--max-real-segments` flag, recorded in the manifest). Only needed if D2 keeps the full dataset and training runs out of time or memory. *(E5.3 plan, step 5)*
- [ ] **D5. Lower `--max-train-segments` in E5.5** if training the anomaly model is too slow on long real segments. *(E5.5 plan, risks)*
- [x] **D6. Add `mapie` as a dependency.** Done (`mapie>=1.6.0`, in `pyproject.toml` and `uv.lock`, uncommitted). It goes into the E5.3 commit.

### Things only Joshua can provide

- [ ] **D7. OpenRouter API key and model ID for E5.6.** Don't create a paid account yourself. The key stays in the shell or a Codespaces secret and is never committed.
  Model: ______
- [ ] **D8. Fallback if the Chronos-2 weights won't download.** If that happens, E5.2 commits `cv` only, and E5.4 and E5.7 use `--no-chronos`. Is that acceptable for the PR?

### Results to accept "as measured"

These can't be tuned. Sign off once you've seen the numbers.

- [ ] **D9. E5.2:** which forecaster wins at each horizon. Note any shift between the 10 training days and the 4 held-out days (the held-out days are much smaller files).
- [ ] **D10. E5.3:** the `aerolink-gbm` errors compared with `cv`/`chronos`, and coverage against the 80% target (70–85% is healthy).
- [ ] **D11. E5.4:** Chronos compared with the baselines. Chronos may lose to `tod_mean` on synthetic data. The guide's expected count is 4,217 departures; check whether the network gives a different number.
- [ ] **D12. E5.5:** precision, recall and latency compared with the fixed 300 m bound, given the small sample (~40 flights).
- [ ] **D13. E5.6:** the LLM pass rate. Known gap: numbers written as words ("three minutes") aren't checked.
- [ ] **D14. E5.7:** the B1 vs A5 line. Several identical rows are expected and are a valid result. Whether to add heavier-traffic scenarios is a separate team decision.

### Limits to state in the PR

- [ ] **D15.** `recalculate` (E5.5) is tested but not called live by the simulator. That wiring belongs to E6.1.
- [ ] **D16.** Demand data is synthetic. The 27% Central Florida share is an assumption.
- [x] **D17.** The E5.7 docs script was checked against the current docs. The 3 edits E5.1 already made print `already done`, and every other edit finds its text, so it won't stop.

## 2. Reading list (in order)

| # | Read | Why | Time |
|---|---|---|---|
| 1 | This file, section 1 | The decisions | 5 min |
| 2 | [e5.1/PLAN.md](e5.1/PLAN.md) | Blocking issues D1–D3 | 5 min |
| 3 | `docs/PLAN.md` § M5 | The official stories and acceptance criteria | 5 min |
| 4 | [e5.2/SPEC.md](e5.2/SPEC.md), [e5.3/SPEC.md](e5.3/SPEC.md) | The forecasting chain, and the day split everything reuses | 15 min |
| 5 | [e5.4/SPEC.md](e5.4/SPEC.md) | Synthetic demand assumptions (FDOT scaling) | 5 min |
| 6 | [e5.5/SPEC.md](e5.5/SPEC.md) | The detection rule and how it's scored | 10 min |
| 7 | [e5.6/SPEC.md](e5.6/SPEC.md) | The LLM safety contract | 5 min |
| 8 | [e5.7/SPEC.md](e5.7/SPEC.md) | The ablation configs and the M5 exit criteria | 5 min |
| 9 | The other `PLAN.md` files | Step by step, read when starting each epic | as needed |
| 10 | `docs/Carla/M5_GUIDE.md` | Full code for every file. Reference only | — |

## 3. Build order

E5.1 (D1–D3 first), then E5.2, then E5.3, then E5.5, then E5.7. E5.4 and E5.6 don't depend on the others and can be built at any point before E5.7.
