#pragma once

#include "duckdb/planner/filter/table_filter_functions.hpp"
#include "oasis_scan.hpp"

namespace duckdb {

// The Bloom filters of the FilterStage are DuckDB's (table_filter_bloom_function.cpp): one 64 bit
// word per key, the word of the low bits of the key's hash, in which the key sets 4 bits. The OR of
// the two halves of such a filter is the filter of half the size, so it folds to any room in the
// lookup table. The hardware hashes like DuckDB (Hash of hash.hpp): an INTEGER key as its 32 bit
// zero extension, a BIGINT key as it is, also in a chunk narrowed to INT32 (FilterSpec::hash64).

// Inserts the hash of a key into the Bloom filter `words` (a power of two), as DuckDB inserts it.
void BloomInsert(std::vector<uint64_t> &words, hash_t hash);

// DuckDB's Bloom filter of a hash join on a column of the scan type, folded to at most max_words
// words (a power of two, 0 for no room), or nullptr when it does not fit, passes about every value,
// or its copy does not agree with BloomFilter::LookupOne. The interval is the range of the type.
std::shared_ptr<const HwBitmap> JoinBloom(ClientContext &context, const BloomFilter &filter,
                                          const LogicalType &scan_type, uint64_t max_words);

// The Bloom filter of the num_keys keys of a prefix range filter over [min, max] that BloomInsert
// put into `words`, folded to the size DuckDB gives a filter of that many keys
// (BloomFilter::GetNumberOfSectors); nullptr when it passes about every value.
std::shared_ptr<const HwBitmap> KeysBloom(int64_t min, int64_t max, const std::vector<uint64_t> &words,
                                          uint64_t num_keys);

} // namespace duckdb
