#pragma once

#include "duckdb/planner/expression.hpp"
#include "oasis_scan.hpp"

namespace duckdb {

// The bitmap of a prefix range filter or an IN list on the column, or nullptr for another expression
// and for a filter that stays on the CPU. An optional IN list may fold. The bitmap fits the room the
// table has now.
std::shared_ptr<const HwBitmap> LookupBitmap(oasis::FilterTable &table, const Expression &expr,
                                             const LogicalType &scan_type, bool optional);

} // namespace duckdb
