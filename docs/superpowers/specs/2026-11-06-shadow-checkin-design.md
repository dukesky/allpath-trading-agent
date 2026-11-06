# Shadow daily check-in and views ledger: design (2026-11-06)

**Status:** pilot, shadow only. Tian approved it through the coordinator on
2026-11-06. Paper is untouched because of the paper-2 experiment.

## Problem

Tian rarely chats with the agent. The rules and the nightly reflection run
without his views, so revisions drift away from what he actually thinks.
Telegram already carries free text into the shadow chat agent. What is
missing is (1) something that asks him a question, and (2) a durable,
structured record of his answers that reflection has to answer to.

## Behaviour

1. **Ask.** After the nightly chain finishes shadow's reflection and
   consolidation, `DailyCheckin` builds a short deterministic fact sheet for
   shadow:
   - today's trades;
   - rule triggers;
   - the biggest movers in the book;
   - pending reviews, including tonight's revision proposals;
   - active views that expire soon.

   The chat-tier LLM turns that into 1 to 3 questions (JSON). They go to the
   paired Telegram chat as one `[Shadow]` message with `force_reply`, and the
   check-in is stored in `checkins` (ET date, questions, Telegram message id).
   It runs at most once per ET day. If Telegram is not paired, it is skipped
   and nothing is recorded. If the LLM fails, the step returns False and the
   nightly retry tries again.
2. **Answer.** When Tian replies to the check-in message, the poller routes
   the reply to the **shadow** ChatService, regardless of the chat's current
   `/account` setting. The turn text is framed with the questions so the
   agent knows what is being answered. A free-text message that is not a
   reply follows normal routing, which defaults to shadow.
3. **Push back and file.** With the flag on, shadow's chat agent gets:
   - a check-in note in its system prompt: check claims against data with the
     read-only tools, push back plainly where the view looks weak, then
     record the view;
   - two tools:
     - `record_view(subject, stance, horizon, conviction, invalidation,
       expires_days?)` files a dated row in `views`;
     - `list_views()` shows the active ones.

   A view records Tian's position even after pushback. The agent's
   disagreement goes in its reply, not in the view.
4. **Anchor.** Shadow's reflection briefing gets an "Owner's active views"
   block. `propose_strategy_revision` then requires a `view_alignment` value
   (`agree` / `extend` / `contradict` plus one line) whenever an active view
   matches the strategy's ticker, or is a market-wide view (subject
   `MARKET`). The alignment is prefixed to the rationale, so the Pending card
   shows it with no template change. A missing alignment is a tool error the
   agent can correct within its budget.

## Configuration

`SHADOW_DAILY_CHECKIN` (bool, default `false`, .env only). There is no paper
equivalent, so paper cannot run the pilot by misconfiguration. With the flag
off, nothing changes:

- no tables are read;
- no tools or prompt lines are added;
- the reflection briefing and the tool schema are byte-identical.

## Data

- `views (id, account, ts, subject, stance, horizon, conviction,
  invalidation, source, expires_ts)`. Subject is an upper-cased ticker or a
  theme. `MARKET` means market-wide. Default expiry is 30 days, maximum 180.
- `checkins (id, account, et_date, ts, questions, telegram_message_id)`,
  UNIQUE `(account, et_date)`.

## Not in scope

- Voice notes (option B).
- Any paper wiring.
- A web UI for views (the agent tools and the DB are enough for the pilot).
- Changing what reflection is allowed to propose.

## Risks

- **Question fatigue:** capped at 3 questions, one message per day, and
  skipping is fine.
- **Stale views:** they expire.
- **Wrong subject match:** matching is by ticker only, plus `MARKET`.
  Themes are visible to reflection in the briefing but do not force an
  alignment.
