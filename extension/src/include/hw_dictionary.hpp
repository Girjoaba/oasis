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

// Reads the dictionary of every dictionary id column of `pending`'s row group and evaluates the conjuncts of
// the column over its entries (pending.dictionaries, pending.dict_id_filters). A conjunct every entry
// passes needs no row filter. Returns false when a required conjunct passes no entry: the group has no
// row.
bool PrepareDictIdColumns(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                          const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending);

// Removes the rows of the selection whose dictionary id fails `filter`, for the vector of a dictionary id column.
void FilterDictIds(const Vector &vec, const DictIdFilter &filter, SelectionVector &sel, idx_t &approved_tuple_count);

} // namespace duckdb
