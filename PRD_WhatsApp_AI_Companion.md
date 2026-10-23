# PRD: WhatsApp AI Companion ("Baby I Need Space")

> Source: transcript of the YouTube video "I Built the WhatsApp AI That Only a Psychopath Will Make" (youtu.be/kFhAE12699w).
> Items marked **[ASSUMED]** were not stated in the video. I chose them to fill gaps.

## 1. Overview
A backend service that connects to WhatsApp through Meta's official Cloud API and does two things:
1. **Auto-replies** to incoming messages in the *owner's own texting style*, using RAG over the owner's exported chat history.
2. **Sends scheduled messages** (for example daily "good morning" / "good night").

A small dashboard configures contacts, schedules, and chat-history uploads. It has no business logic of its own.

## 2. Goals / Non-goals
| Goals | Non-goals (v1) |
|---|---|
| Receive WhatsApp messages via webhook and reply automatically | Polished frontend (dashboard is config-only) |
| Reply in the owner's tone/language (Hinglish, slang) using RAG | Multi-user SaaS / billing |
| Per-contact memory and conversation history | Voice notes, images, group chats |
| Scheduled outbound messages | Production-grade business verification |
| Free or cheap LLM (Gemini / Groq) | Fine-tuning a model |

## 3. Users
- **Owner**: one person (personal use) who configures the bot, uploads chats, and sets schedules.
- **Contact**: a person who texts the owner's WhatsApp number and receives the bot's replies.

## 4. Tech Stack
| Layer | Choice | Source |
|---|---|---|
| Messaging | Meta WhatsApp Business **Cloud API** (Meta Developer app, WhatsApp use case, test number) | Video |
| Inbound transport | **Webhook** (Meta server POSTs to our server), subscribed to the `messages` field | Video |
| Local tunnel | **ngrok** (static URL for the webhook during local dev) | Video |
| Backend | **Python**, entry file `main.py` | Video; web framework **[ASSUMED]**: FastAPI |
| Scheduler | **APScheduler `BackgroundScheduler`** for v1; **Celery + broker** noted as the production upgrade | Video |
| Relational DB | **PostgreSQL** (contacts, messages, memories) | Video |
| Vector DB | **Pinecone** (chat-history embeddings) | Video |
| LLM | **Gemini** or a free model on **Groq** (swappable) | Video |
| Embeddings | **[ASSUMED]** Gemini `text-embedding` model, or any embedding API |
| Frontend | Minimal dashboard (config only) | Video; framework **[ASSUMED]**: plain HTML/React |

## 5. Architecture
```
Contact ──WhatsApp──► Meta Cloud API ──POST /webhook──► [ngrok] ──► FastAPI
                                                                      │
                          ┌───────────────────────────────────────────┤
                          ▼                                           ▼
                    PostgreSQL                                 RAG pipeline
              (contacts/messages/memories)          embed msg → Pinecone top-k chunks
                          │                                           │
                          └────────────► build prompt ◄───────────────┘
                                              │
                                              ▼
                                      LLM (Gemini/Groq)
                                              │
                          Meta Graph API  ◄───┘  send reply
APScheduler ──(cron time)──► send scheduled message via Graph API
Dashboard ──► REST API (contacts, schedules, chat upload)
```

## 6. Functional Requirements
| ID | Requirement | Priority |
|---|---|---|
| FR-1 | `GET /webhook` answers Meta's verification handshake (`hub.challenge`, verify token) | P0 |
| FR-2 | `POST /webhook` parses incoming message events, ignores status updates, and returns 200 quickly | P0 |
| FR-3 | Store every inbound/outbound message in Postgres, linked to a contact | P0 |
| FR-4 | Generate a reply: recent conversation history + retrieved owner-style chunks + persona prompt → LLM | P0 |
| FR-5 | Send the reply through the WhatsApp Graph API | P0 |
| FR-6 | Upload a WhatsApp chat export (`.txt`). Parse it, chunk it, embed it, and upsert it to Pinecone | P0 |
| FR-7 | Retrieval: embed the incoming message, query Pinecone top-k, then rank and filter chunks by relevance | P0 |
| FR-8 | Scheduler: create, list, and delete a recurring or one-time message per contact (for example 09:00 daily) | P1 |
| FR-9 | Per-contact settings: bot on/off, persona notes, reply delay, schedule list | P1 |
| FR-10 | Long-term "memories" per contact (facts to remember), editable in the dashboard | P1 |
| FR-11 | Dashboard to view contacts, chats, and schedules, and to upload history | P1 |
| FR-12 | Kill-switch: one command or toggle disables all auto-replies | P0 |

## 7. Data Model (PostgreSQL)
```sql
contacts(id serial pk, wa_number text unique, name text, bot_enabled bool default true, persona_notes text);
messages(id serial pk, contact_id int fk, direction text check (direction in ('in','out')), body text, wa_msg_id text unique, created_at timestamptz default now());
memories(id serial pk, contact_id int fk, fact text, created_at timestamptz default now());
schedules(id serial pk, contact_id int fk, cron text, body text, enabled bool default true);
```
Pinecone: index `chat-style`, metadata `{source, chunk_id, ts}`, one vector per chunk.

## 8. RAG Pipeline
1. **Ingest**: parse the export (format varies by locale, so use a tolerant regex), keep only the *owner's* lines plus the preceding message for context, and chunk by conversation window (about 5–10 turns).
2. **Embed + upsert** the chunks to Pinecone.
3. **Query time**: embed the incoming message, fetch top-k (k≈5), and drop chunks below a similarity threshold.
4. **Prompt**: `system persona` + `retrieved style examples` + `contact memories` + `last N messages` + `incoming message`.
5. **Generate** a short, casual reply that matches the owner's language and slang.

Known weakness (stated in the video): retrieval quality drops as data grows. Improvements for later are better chunking, re-ranking, and metadata filters.

## 9. API (internal)
| Method | Path | Purpose |
|---|---|---|
| GET | `/webhook` | Meta verification |
| POST | `/webhook` | Receive messages |
| POST | `/chat-history` | Upload an export and ingest it |
| GET/POST/PATCH | `/contacts` | Manage contacts and settings |
| GET/POST/DELETE | `/schedules` | Manage scheduled messages |
| POST | `/send` | Manual send (testing) |

## 10. Configuration (`.env`)
```
WA_PHONE_NUMBER_ID=  WA_ACCESS_TOKEN=  WA_VERIFY_TOKEN=  WA_APP_SECRET=
DATABASE_URL=postgresql://...
PINECONE_API_KEY=  PINECONE_INDEX=chat-style
LLM_PROVIDER=gemini|groq  GEMINI_API_KEY=  GROQ_API_KEY=
```
Never commit `.env`. Hide phone numbers and tokens in any screenshots.

## 11. Project Structure
```
app/ main.py  webhook.py  whatsapp.py  llm.py  rag.py  scheduler.py  db.py  models.py
dashboard/
scripts/ ingest_chat.py
.env.example  requirements.txt  README.md
```

## 12. Build Plan (video says ~2 hours for the prototype)
| Phase | Task | Est. |
|---|---|---|
| 1 | Meta Developer app, test number, access token | 20 min |
| 2 | FastAPI skeleton, `/webhook` verification, ngrok | 20 min |
| 3 | Postgres schema, message logging | 15 min |
| 4 | LLM reply (no RAG) and Graph API send | 20 min |
| 5 | Chat export parser, embeddings, Pinecone, retrieval | 30 min |
| 6 | APScheduler scheduled messages | 15 min |
| 7 | Minimal dashboard | 20 min |
| 8 | Testing and prompt tuning | 20 min |

```
Phase  0    30   60   90   120  150  180
1 Meta |███|
2 Hook |   ██|
3 DB   |     █|
4 LLM  |      ██|
5 RAG  |        ███|
6 Sched|           █|
7 UI   |            ██|
8 Test |              ██|
```

## 13. Constraints and Risks
| Risk | Mitigation |
|---|---|
| Cloud API **24-hour window**: free-form messages are only allowed within 24h of the contact's last message. Scheduled messages outside it need **pre-approved templates**. | Use template messages for scheduled sends, or only send within the window |
| Test number can message only a few pre-verified recipients | Add recipients in the Meta dashboard; plan business setup for real use |
| Cloud API is built for business messaging; personal impersonation may conflict with WhatsApp/Meta terms | Review the terms before real use |
| Free LLMs give low-quality replies (video author saw this) | Make the provider swappable; improve prompt and retrieval |
| Webhook retries cause duplicate replies | Dedupe on `wa_msg_id` |
| Webhook must respond fast | Return 200 immediately; process in a background task |
| Server down means lost scheduled jobs (BackgroundScheduler is in-process) | Move to Celery plus a broker for production |
| **Deception / consent**: the bot talks to real people as the owner | Disclose to contacts, or limit to consenting friends. Keep the kill-switch. |
| Private chat exports go to third-party APIs | Redact sensitive data and get consent from people in the chats |

## 14. Acceptance Criteria
- [ ] A message sent to the bot number arrives at `/webhook` and is logged.
- [ ] The bot replies within 10 seconds in a style close to the owner's.
- [ ] Uploading a chat export makes replies visibly more like the owner (A/B check).
- [ ] A scheduled 09:00 message is delivered at 09:00.
- [ ] Duplicate webhook deliveries do not cause duplicate replies.
- [ ] The kill-switch stops all auto-replies immediately.

## 15. Future Improvements (from the video's outro)
Better chunking and re-ranking, a stronger model, Celery-based persistent scheduling, and richer memory.

## 16. Prompt for Antigravity
> Build the project described in this PRD. Start with phases 1–4 (FastAPI webhook, Postgres logging, LLM reply, Graph API send). Use `.env` for secrets. Write compact code. Ask before adding libraries not listed in section 4.
