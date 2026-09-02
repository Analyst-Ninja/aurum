{{ config(materialized='view') }}

select
    *
from public.ohlcv_1d