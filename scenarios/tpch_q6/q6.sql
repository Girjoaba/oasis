SET oasis_rdma_server = 'sim';
SET oasis_logging_level = 'INFO';
SELECT count(*) FROM read_oasis('rdma://lineitem.parquet');
SELECT sum(l_extendedprice * l_discount) AS revenue
FROM read_oasis('rdma://lineitem.parquet')
WHERE l_shipdate >= DATE '1994-01-01' AND l_shipdate < DATE '1995-01-01'
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
SELECT sum(l_extendedprice * l_discount) AS revenue_native
FROM read_parquet('lineitem.parquet')
WHERE l_shipdate >= DATE '1994-01-01' AND l_shipdate < DATE '1995-01-01'
  AND l_discount BETWEEN 0.05 AND 0.07 AND l_quantity < 24;
SELECT * FROM oasis_stream_profile();
