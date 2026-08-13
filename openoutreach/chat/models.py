# openoutreach/chat/models.py
#
# Intentionally model-less. This app owned `ChatMessage`, the per-Deal
# conversation, which was a pre-pivot leftover: it existed because a LinkedIn turn
# was not an RFC-5322 message. In an email-only product a turn *is* a message, so
# keeping a second table meant dual-writing a conversation and a transport record
# that could disagree — and they did.
#
# It is absorbed into `emails.Message` (migration 0006 here, backfilled by
# `emails` first): `external_id` → `message_id`, `is_outgoing` → `direction`,
# `creation_date` → `sent_at`, `content` → `body_text`, `deal` → via `thread`.
# `answer_to` and `topic` were self-FKs nobody has set since the pivot and are
# not carried over. The app stays installed to anchor migration history.
