-- Owner-approved private dialogs/bot support, 2026-10-13. No new tables or columns.
alter table tg_chat drop constraint if exists tg_chat_kind_check;
alter table tg_chat add constraint tg_chat_kind_check
  check (kind in ('group','supergroup','forum','channel','private','bot'));
alter table tg_chat drop constraint if exists tg_chat_peer_type_check;
alter table tg_chat add constraint tg_chat_peer_type_check
  check ((kind in ('private','bot') and chat_id > 0)
      or (kind in ('group','supergroup','forum','channel') and chat_id < 0));
