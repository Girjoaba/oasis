#pragma once

#include "duckdb/planner/expression.hpp"
#include "oasis_scan.hpp"

namespace duckdb {

// Sets `bit` of the words of a bitmap.
void SetBit(std::vector<uint64_t> &words, uint64_t bit);

// The bitmap of `words` over [min, max]. A bitmap that sets every bit, or that is not exact and sets
// about every bit, keeps no words: its interval goes to the hardware alone.
std::shared_ptr<const HwBitmap> MakeBitmap(int64_t min, int64_t max, uint32_t shift, bool exact,
                                           std::vector<uint64_t> words);

// The bitmap of a prefix range filter or an IN list on the column, or nullptr for another expression
// and for a filter that stays on the CPU. An optional IN list may fold. The bitmap fits the room the
// table has now.
std::shared_ptr<const HwBitmap> LookupBitmap(oasis::FilterTable &table, const Expression &expr,
                                             const LogicalType &scan_type, bool optional);

} // namespace duckdb
