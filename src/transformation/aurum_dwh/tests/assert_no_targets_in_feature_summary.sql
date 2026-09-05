-- A target column reaching `mart_feature_summary` outside the declared key set means
-- `selected_features.csv` carries one, and every model trained on the mart afterwards
-- is fitted on the answer. The seed writer asserts the same thing in Python; this is
-- the second layer, on the side of the boundary the model actually reads from.
--
-- The key columns are projected on purpose — they carry the targets a training set
-- needs — so they are excluded by name rather than by pattern.

{% set relation = ref('mart_feature_summary') %}

select column_name
from information_schema.columns
where table_schema = '{{ relation.schema }}'
  and table_name = '{{ relation.identifier }}'
  and (column_name like 'fwd\_ret%' or column_name like 'label%')
  and column_name not in (
      'fwd_ret_5d',
      'fwd_ret_21d',
      'fwd_ret_5d_excess',
      'fwd_ret_5d_xs_decile',
      'label_up_5d'
  )
