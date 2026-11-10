# Indication checker evaluation

The second reader of indications (`prompts/check_indication/`, verifier role) is scored on `cases.jsonl`: planted sentences, each with a drug, an event, a sentence, a disease written in that sentence, and whether the sentence states the drug is aimed at that disease for that event (`true`).

- Ten cases come from real gold-set releases; the other 29 are sentences reviews used to break plain-code checks: another drug's disease, side effects, excluded patients, earlier or failed results, a disease that only causes the condition treated, another program of the same drug, a disease followed by the symptom treated ("Parkinson's disease agitation"), and a planted instruction.
- Every case writes its disease in its sentence, so the plain-code checks pass and only the checker decides.

Scores (`catalyst eval-extractor --prompt check_indication --out scores.json`, one model call per case):

- `accepts_true`: of true cases, the share it confirms.
- `refuses_false`: of false cases, the share it refuses. A false case it confirms would let a wrong indication publish.

Current scores (`scores.json`, run 42906248003): accepts_true 1.0 (13 of 13), refuses_false 0.962 (25 of 26). The one false case it confirms (`psc_is_the_cause`) is also refused by plain code, which refuses a disease the sentence gives as a cause.
