#pragma once

#include "duckdb/planner/expression.hpp"
#include "oasis_scan.hpp"

namespace duckdb {

// A folded bitmap with more set bits than this share passes about as much as its [min, max]: the
// interval goes to the hardware alone and the bitmap keeps no words. Also the largest share of the
// values outside its keys that a Bloom filter may pass (hw_bloom.cpp).
static constexpr double FOLDED_BITMAP_MAX_ONES = 0.9;

// Sets `bit` of the words of a bitmap.
void SetBit(std::vector<uint64_t> &words, uint64_t bit);

// The bitmap of `words` over [min, max]. A bitmap that sets every bit, or that is not exact and sets
// about every bit, keeps no words: its interval goes to the hardware alone. HwBitmap::exact holds for
// a bitmap of shift 0 that keeps its words or sets every bit.
std::shared_ptr<const HwBitmap> MakeBitmap(int64_t min, int64_t max, uint32_t shift, bool exact,
                                           std::vector<uint64_t> words);

// Whether the prefix range or Bloom filter function `expr` of a hash join reads a BIGINT column through
// the cast of a compressed join key, TRY_CAST(column AS INTEGER) (runtime_filter_cast.cpp of DuckDB).
// Its keys are INTEGER values: the filter holds for a chunk only if every value fits, where the cast is
// the identity (HwScanFilter::int32_key).
bool ReadsInt32Key(const Expression &expr, const LogicalType &scan_type);

// The bitmap of a prefix range filter or an IN list on the column, with bloom also the Bloom filter
// of a hash join (hw_bloom.hpp), or nullptr for another expression and for a filter that stays on the
// CPU. An optional IN list may fold; with bloom a prefix range filter that folds may go as the Bloom
// filter of its keys. The prefix range and Bloom filters may read the column through the cast of
// ReadsInt32Key; their bitmaps are then over INTEGER keys. The bitmap fits the room the table has now.
std::shared_ptr<const HwBitmap> LookupBitmap(ClientContext &context, oasis::FilterTable &table, const Expression &expr,
                                             const LogicalType &scan_type, bool optional, bool bloom);

} // namespace duckdb
