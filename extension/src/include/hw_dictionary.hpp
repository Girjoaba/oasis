#pragma once

#include "oasis_scan.hpp"

namespace duckdb {

// The decoders emit the ids of a dictionary-encoded string chunk; this bounds the dictionary.
static constexpr idx_t MAX_DICTIONARY_IDS = idx_t(1) << 19;

// Fills bind.dict_chunks: per row group and file column, the chunk of a string column without NULLs
// whose data pages are all dictionary-encoded (encoding_stats, else no PLAIN in encodings).
void BuildDictionaryChunks(ParquetReader &reader, OasisScanBindData &bind);

// Prepares the dictionary id filters of the worker's scan filters (OasisScanFilter::dict_id_filter).
void InitDictIdFilters(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate);

// The dictionaries of the dictionary id columns of `pending`'s row group (pending.dictionaries): from DuckDB's
// object cache (oasis_dict_cache), keyed by the file (OasisScanLocalState::dict_cache_file) and the offset and
// size of the page, else read through the worker's file handle, or with `fetch` (RDMA) listed in
// pending.dict_slot for a splinter to fetch, without blocking the worker. Returns whether the conjuncts of a
// listed column need its dictionary before the group is submitted (PrepareDictIdColumns).
bool LookupDictionaries(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                        const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending, bool fetch);

// Decodes the dictionary pages a splinter fetched (pending.dict_slot, pending.dict_buffers) into
// pending.dictionaries and puts them in the cache.
void DecodeFetchedDictionaries(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                               const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending);

// Evaluates the conjuncts of every dictionary id column of `pending`'s row group over the entries of its
// dictionary (pending.dict_id_filters), which LookupDictionaries or DecodeFetchedDictionaries put in
// pending.dictionaries. A conjunct every entry passes needs no row filter. Returns false when a required
// conjunct passes no entry: the group has no row.
bool PrepareDictIdColumns(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                          const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending);

// Removes the rows of the selection whose dictionary id fails `filter`, for the vector of a dictionary id column.
void FilterDictIds(const Vector &vec, const DictIdFilter &filter, SelectionVector &sel, idx_t &approved_tuple_count);

} // namespace duckdb
