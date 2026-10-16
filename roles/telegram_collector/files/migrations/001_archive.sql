create extension if not exists pg_trgm;

create table if not exists sync_run (
  id          bigserial primary key,
  started_at  timestamptz not null,
  finished_at timestamptz,
  status      text not null check (status in ('running','ok','partial','failed')),
  counts      jsonb not null default '{}',  -- по чатам: fetched, upserted, deleted, service_skipped,
                                            -- redacted, media_uploaded, media_bytes, media_failed
  error       text
);

create table if not exists tg_chat (
  chat_id         bigint primary key,        -- peer id Telethon (-100… для супергрупп и каналов)
  slug            text not null unique,      -- из chats.yaml
  title           text not null,
  project         text,                      -- из chats.yaml, свободный текст для LLM
  kind            text not null check (kind in ('group','supergroup','forum','channel')),
  public_username text,                      -- только для публичных чатов, для ссылок
  protected       boolean not null default false, -- в чате запрещено сохранение контента (E22)
  since           date not null,             -- нижняя граница выгрузки из chats.yaml
  cursor_msg_id   bigint not null default 0, -- максимальный закоммиченный msg_id
  backfill_done   boolean not null default false,
  status          text not null check (status in
                    ('new','backfilling','ok','lost_access','rejected','failed')),
  last_ok_at      timestamptz,
  last_attempt_at timestamptz,
  last_error      text
);

create table if not exists tg_user (
  user_id      bigint primary key,
  display_name text not null,                -- имя и фамилия из профиля на момент сбора
  username     text,
  phone        text,                         -- только если Telegram его отдаёт (контакт или
                                             -- открытые настройки приватности), иначе null
  updated_at   timestamptz not null
);

create table if not exists tg_topic (
  chat_id  bigint not null references tg_chat(chat_id) on delete cascade,
  topic_id bigint not null,
  title    text not null,
  primary key (chat_id, topic_id)
);

create table if not exists tg_message (
  chat_id        bigint not null references tg_chat(chat_id) on delete cascade,
  msg_id         bigint not null,
  topic_id       bigint,                     -- для форумов
  sent_at        timestamptz not null,
  edited_at      timestamptz,
  sender_id      bigint,                     -- null: от имени чата или анонимного админа
  reply_to_id    bigint,
  text           text not null,              -- после маскировки секретов (E8); скрытые ссылки сохранены
  redactions     smallint not null default 0,
  -- вложение (в Telegram у сообщения не больше одного; альбом — это несколько сообщений).
  -- Метаданные пишутся для КАЖДОГО вложения при текстовом проходе, независимо от того,
  -- будет ли файл скачан (E25).
  media_kind     text,                       -- document|photo|video|audio|voice|video_note|sticker|gif|other
  media_name     text,                       -- исходное имя файла; для фото и голосовых — null
  media_ext      text,                       -- расширение в нижнем регистре без точки (правило ниже)
  media_mime     text,
  media_size     bigint,                     -- байты, из метаданных Telegram
  media_file_id  bigint,                     -- id документа или фото в Telegram; меняется при замене файла
  media_status   text check (media_status in
                   ('pending','done','failed','skipped_kind','skipped_too_big',
                    'skipped_protected','skipped_disabled','skipped_quota')),
  media_error    text,                       -- класс последней ошибки для failed: flood_wait,
                                             -- file_reference_expired, timeout, sftp_error, size_mismatch, other
  media_path     text,                       -- путь относительно корня папки вложений на сервере
  media_sha256   text,                       -- считается на лету при передаче
  media_attempts smallint not null default 0,
  fetched_at     timestamptz not null,
  tsv tsvector generated always as
    (to_tsvector('russian', text || ' ' || coalesce(media_name, ''))) stored,
  primary key (chat_id, msg_id)
);

create index if not exists tg_message_tsv on tg_message using gin (tsv);
create index if not exists tg_message_text on tg_message using gin (text gin_trgm_ops);
create index if not exists tg_message_media_name on tg_message using gin (media_name gin_trgm_ops) where media_name is not null;
create index if not exists tg_message_date on tg_message (chat_id, sent_at);
create index if not exists tg_message_topic on tg_message (chat_id, topic_id, msg_id);
create index if not exists tg_message_ext on tg_message (media_ext) where media_ext is not null;
create index if not exists tg_message_pending on tg_message (media_status) where media_status = 'pending';
