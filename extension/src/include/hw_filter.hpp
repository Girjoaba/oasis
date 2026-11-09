#pragma once

#include "duckdb/planner/expression.hpp"
#include "oasis_scan.hpp"

namespace duckdb {

// The stored integer of a constant of the scan type, which is what the decoder emits and the hardware
// compares, or nullopt for another expression or type.
std::optional<int64_t> StoredConstant(const Expression &constant, const LogicalType &scan_type);

// What the hardware takes of each filter of BuildScanFilters on gstate.filters, in its order: the
// constant comparisons and IN lists of the query and the bounds and prefix range bitmaps of the hash
// joins. Builds the bitmaps once per scan; their regions of the lookup table are taken by
// PlanHardwareFilter (HwBitmap::Region).
std::vector<HwScanFilter> ClassifyHardwareFilters(ClientContext &context, const OasisScanGlobalState &gstate,
                                                  const OasisScanBindData &bind);

// Decides whether `pending`'s row group is filtered in hardware and plans it: the FilterSpec of
// every hardware column chunk, the order the chunks go to the decoder in, which columns the
// hardware emits and which conjuncts it takes (their needs_row_filter entries become false, except
// for a conjunct of an emitted column that the hardware passes a superset of, HwScanFilter::superset).
// Returns false, leaving pending as it is, when the group takes the CPU path: no conjunct the
// hardware can take, only optional join bounds that the statistics expect to keep more rows than
// DuckDB's threshold of the filter (HwScanFilter::threshold), more
// rows than the selection bitmap holds, or an emitted column of a type the hardware cannot select.
// Takes the table region of a bitmap the group looks up first.
bool PlanHardwareFilter(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                        const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending);

} // namespace duckdb
