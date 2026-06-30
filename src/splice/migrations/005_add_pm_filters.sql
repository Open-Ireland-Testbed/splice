create table if not exists pm_filters (
    id bigserial primary key,
    device_id bigint not null references devices(id) on delete cascade,
    root_path text not null,
    filter_xml text not null,
    created_at timestamp with time zone not null default now()
);

create unique index if not exists ix_pm_filters_device_root
    on pm_filters(device_id, root_path);
