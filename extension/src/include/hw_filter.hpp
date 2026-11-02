#pragma once

#include "oasis_scan.hpp"

namespace duckdb {

// Fills OasisScanFilter::hw_interval of every scan filter.
void ClassifyHardwareFilters(ClientContext &context, const OasisScanGlobalState &gstate, const OasisScanBindData &bind,
                             std::vector<OasisScanFilter> &scan_filters);

// Decides whether `pending`'s row group is filtered in hardware and plans it: the FilterSpec of
// every hardware column chunk, the order the chunks go to the decoder in, which columns the
// hardware emits and which conjuncts it takes (their needs_row_filter entries become false).
// Returns false, leaving pending as it is, when the group takes the CPU path: no conjunct the
// hardware can take, more rows than the selection bitmap holds, or an emitted column of a type
// the hardware cannot select.
bool PlanHardwareFilter(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                        const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending);

} // namespace duckdb
