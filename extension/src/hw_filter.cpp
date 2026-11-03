#include "hw_filter.hpp"

#include "column_reader.hpp"
#include "duckdb/common/limits.hpp"
#include "duckdb/logging/log_manager.hpp"
#include "duckdb/planner/expression/bound_between_expression.hpp"
#include "duckdb/planner/expression/bound_comparison_expression.hpp"
#include "duckdb/planner/expression/bound_constant_expression.hpp"
#include "duckdb/planner/expression/bound_function_expression.hpp"
#include "duckdb/planner/filter/expression_filter.hpp"
#include "duckdb/planner/filter/table_filter_functions.hpp"
#include "duckdb/storage/statistics/numeric_stats.hpp"
#include "parquet_reader.hpp"

#include <algorithm>
#include <sstream>

namespace duckdb {

static constexpr int64_t INT64_LO = std::numeric_limits<int64_t>::min();
static constexpr int64_t INT64_HI = std::numeric_limits<int64_t>::max();

// No value passes lo <= x <= hi
static constexpr HwInterval EMPTY_INTERVAL {1, 0, false};

// Whether the FilterStage selects values of the type at 32 bit lane granularity: INT32 and INT64
// (also FLOAT and DOUBLE, which it can pass along a selection but not compare). BYTE and INT16
// columns pass through uncompacted, so a row group that emits one cannot be filtered in hardware.
static bool HwFilterLaneType(libstf::type_t transfer_type) {
	const size_t size = libstf::size_of(transfer_type);
	return size == sizeof(int32_t) || size == sizeof(int64_t);
}

// Whether the FilterCompare can compare values of the type.
static bool HwFilterCompareType(libstf::type_t transfer_type) {
	return transfer_type == libstf::type_t::INT32_T || transfer_type == libstf::type_t::INT64_T;
}

// The stored integer of a constant of the column's type, which is what the decoder emits and the
// hardware compares. Only the types whose stored value orders like the DuckDB value.
static std::optional<int64_t> StoredValue(const Value &value) {
	if (value.IsNull()) {
		return std::nullopt;
	}
	const auto &type = value.type();
	switch (type.id()) {
	case LogicalTypeId::INTEGER:
		return value.GetValueUnsafe<int32_t>();
	case LogicalTypeId::BIGINT:
		return value.GetValueUnsafe<int64_t>();
	case LogicalTypeId::DATE:
		return value.GetValueUnsafe<date_t>().days;
	case LogicalTypeId::TIME:
		return value.GetValueUnsafe<dtime_t>().value;
	case LogicalTypeId::TIMESTAMP:
	case LogicalTypeId::TIMESTAMP_SEC:
	case LogicalTypeId::TIMESTAMP_MS:
	case LogicalTypeId::TIMESTAMP_NS:
		return value.GetValueUnsafe<timestamp_t>().value;
	case LogicalTypeId::DECIMAL:
		switch (type.InternalType()) {
		case PhysicalType::INT32:
			return value.GetValueUnsafe<int32_t>();
		case PhysicalType::INT64:
			return value.GetValueUnsafe<int64_t>();
		default:
			return std::nullopt;
		}
	default:
		return std::nullopt;
	}
}

// The interval of `column <comparison> constant`.
static std::optional<HwInterval> ComparisonInterval(ExpressionType comparison, int64_t constant) {
	switch (comparison) {
	case ExpressionType::COMPARE_EQUAL:
		return HwInterval {constant, constant, false};
	case ExpressionType::COMPARE_NOTEQUAL:
		return HwInterval {constant, constant, true};
	case ExpressionType::COMPARE_LESSTHAN:
		return constant == INT64_LO ? EMPTY_INTERVAL : HwInterval {INT64_LO, constant - 1, false};
	case ExpressionType::COMPARE_LESSTHANOREQUALTO:
		return HwInterval {INT64_LO, constant, false};
	case ExpressionType::COMPARE_GREATERTHAN:
		return constant == INT64_HI ? EMPTY_INTERVAL : HwInterval {constant + 1, INT64_HI, false};
	case ExpressionType::COMPARE_GREATERTHANOREQUALTO:
		return HwInterval {constant, INT64_HI, false};
	default:
		return std::nullopt;
	}
}

// The intersection of two intervals without negate.
static HwInterval Intersect(const HwInterval &a, const HwInterval &b) {
	D_ASSERT(!a.negate && !b.negate);
	HwInterval result {std::max(a.lo, b.lo), std::min(a.hi, b.hi), false};
	return result.lo <= result.hi ? result : EMPTY_INTERVAL;
}

// The stored integer of a constant of the scan type.
static std::optional<int64_t> StoredConstant(const Expression &constant, const LogicalType &scan_type) {
	if (constant.GetExpressionClass() != ExpressionClass::BOUND_CONSTANT) {
		return std::nullopt;
	}
	const auto &value = constant.Cast<BoundConstantExpression>().GetValue();
	if (value.type() != scan_type) {
		return std::nullopt;
	}
	return StoredValue(value);
}

// The interval of a comparison of the column with a constant (also BETWEEN), or nullopt for any other
// expression.
static std::optional<HwInterval> ConstantInterval(const Expression &expr, const LogicalType &scan_type) {
	if (expr.GetExpressionClass() != ExpressionClass::BOUND_FUNCTION) {
		return std::nullopt;
	}
	auto &func = expr.Cast<BoundFunctionExpression>();
	// The dynamic, bloom and prefix range filters of the joins
	if (TableFilterFunctions::IsTableFilterFunction(func.Function())) {
		return std::nullopt;
	}

	if (BoundComparisonExpression::IsComparison(expr)) {
		ExpressionType comparison;
		auto constant = ExpressionFilter::TryGetColumnConstantComparison(func, comparison);
		if (!constant) {
			return std::nullopt;
		}
		// The other side is the column, and the comparison is flipped for a constant on the left
		auto stored = StoredConstant(*constant, scan_type);
		if (!stored) {
			return std::nullopt;
		}
		return ComparisonInterval(comparison, *stored);
	}

	if (expr.GetExpressionType() == ExpressionType::COMPARE_BETWEEN && BoundBetweenExpression::HasValidBindData(func)) {
		if (!ExpressionFilter::IsSimpleFilterColumnRef(BoundBetweenExpression::Input(func))) {
			return std::nullopt;
		}
		auto lower = StoredConstant(BoundBetweenExpression::LowerBound(func), scan_type);
		auto upper = StoredConstant(BoundBetweenExpression::UpperBound(func), scan_type);
		if (!lower || !upper) {
			return std::nullopt;
		}
		auto lower_interval = ComparisonInterval(BoundBetweenExpression::LowerComparisonType(func), *lower);
		auto upper_interval = ComparisonInterval(BoundBetweenExpression::UpperComparisonType(func), *upper);
		return Intersect(*lower_interval, *upper_interval);
	}

	return std::nullopt;
}

// Fills hw_interval and hw_optional of a scan filter on a hardware column. The hardware takes a
// comparison of the column with a constant: a constant filter of the query, the one-key equality of a
// hash join, and the min/max bounds of a hash join, which come as an optional or selectivity-optional
// filter around such a comparison. Everything else stays on the CPU: the dynamic (top-N), bloom and
// prefix range filters, IN, OR, LIKE, a type the hardware does not compare and a column a pushed cast
// narrowed. Independent of the row group.
static void ClassifyHardwareFilter(OasisScanFilter &filter, const ProjectedColumn &col, const LogicalType &scan_type,
                                   libstf::type_t transfer_type) {
	// A column a cast narrowed, or a filter rebound to the narrow type, keeps its filters on the CPU
	if (col.cast_type || filter.row_filter || !filter.prune_on_stats) {
		return;
	}
	if (!HwFilterCompareType(transfer_type)) {
		return;
	}
	if (filter.filter.filter_type != TableFilterType::EXPRESSION_FILTER) {
		return;
	}
	auto &expr_filter = filter.filter.Cast<ExpressionFilter>();
	if (!expr_filter.column_indexes.empty() || !expr_filter.expr) {
		return; // Multi-column filter
	}
	const auto &expr = *expr_filter.expr;
	// The min/max bounds of a join: optional, so the hardware may drop the rows outside or keep them
	auto join_bound = ExpressionFilter::GetOptionalFilterChild(expr);
	if (join_bound) {
		filter.hw_interval = ConstantInterval(*join_bound, scan_type);
		filter.hw_optional = filter.hw_interval.has_value();
		return;
	}
	filter.hw_interval = ConstantInterval(expr, scan_type);
}

// The type the decoder emits for the column before any narrowing of a chunk.
static libstf::type_t TransferType(const OasisScanBindData &bind, const ProjectedColumn &col) {
	return col.cast_type.value_or(bind.metadata.groups[0].chunks[col.column_id].logical_type());
}

void ClassifyHardwareFilters(ClientContext &context, const OasisScanGlobalState &gstate, const OasisScanBindData &bind,
                             std::vector<OasisScanFilter> &scan_filters) {
	for (auto &filter : scan_filters) {
		const auto &col = gstate.projected_columns[filter.filter_idx];
		if (col.is_cpu) {
			continue;
		}
		const auto &scan_type = bind.output_types[col.column_id];
		ClassifyHardwareFilter(filter, col, scan_type, TransferType(bind, col));
		if (filter.hw_interval) {
			DUCKDB_LOG_DEBUG(context, "Hardware filter on column '%s': %s%s[%lld, %lld]",
			                 bind.metadata.column_names[col.column_id].c_str(), filter.hw_optional ? "join " : "",
			                 filter.hw_interval->negate ? "not " : "", (long long)filter.hw_interval->lo,
			                 (long long)filter.hw_interval->hi);
		}
	}
}

// Clamps the interval to the 32 bit lane the hardware compares: the FilterCompare takes the low
// 32 bits of the bounds. Exact for a narrowed chunk because every value of the chunk fits INT32.
static HwInterval FitToLane(const HwInterval &interval, bool lane32) {
	if (!lane32) {
		return interval;
	}
	const int64_t lo = NumericLimits<int32_t>::Minimum();
	const int64_t hi = NumericLimits<int32_t>::Maximum();
	if (interval.lo > hi || interval.hi < lo) {
		return interval.negate ? HwInterval {lo, hi, false} : EMPTY_INTERVAL;
	}
	return HwInterval {std::max(interval.lo, lo), std::min(interval.hi, hi), interval.negate};
}

// The statistics of the column chunk in the group, or null without statistics.
static unique_ptr<BaseStatistics> ChunkStats(OasisScanLocalState &lstate, size_t group, size_t column_id) {
	const auto &pq_columns = lstate.parquet_reader->GetFileMetadata()->row_groups[group].columns;
	return lstate.scan_state->GetColumnReader(column_id).Stats(group, pq_columns);
}

// Whether the column chunk can hold no NULL: the column is REQUIRED or its statistics say so. The
// FilterStage compares the stored values without definition levels, so a NULL would compare as its
// stored value.
static bool ChunkHasNoNulls(OasisScanLocalState &lstate, size_t group, const parcore::metadata::ColumnChunk &cc,
                            size_t column_id) {
	if (cc.is_required) {
		return true;
	}
	auto stats = ChunkStats(lstate, group, column_id);
	return stats && !stats->CanHaveNull();
}

// The share of the chunk's rows that pass the interval if the values spread evenly over the
// [min, max] of the chunk statistics; 1 without statistics.
static double EstimatedPassFraction(OasisScanLocalState &lstate, size_t group, size_t column_id,
                                    const HwInterval &interval) {
	auto stats = ChunkStats(lstate, group, column_id);
	if (!stats || !NumericStats::HasMinMax(*stats)) {
		return 1;
	}
	auto min = StoredValue(NumericStats::Min(*stats));
	auto max = StoredValue(NumericStats::Max(*stats));
	if (!min || !max || *min > *max) {
		return 1;
	}
	const auto overlap = Intersect(HwInterval {interval.lo, interval.hi, false}, HwInterval {*min, *max, false});
	const long double inside = overlap.lo <= overlap.hi
	                               ? ((long double)overlap.hi - overlap.lo + 1) / ((long double)*max - *min + 1)
	                               : 0;
	return double(interval.negate ? 1 - inside : inside);
}

// The FilterSpec as text, for the debug log (formatted only when the log is on).
static std::string SpecText(const oasis::FilterSpec &spec) {
	std::ostringstream text;
	text << spec;
	return text.str();
}

bool PlanHardwareFilter(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                        const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending) {
	if (!gstate.hw_filter || pending.hw_slot.empty() || pending.num_rows > gstate.ctx->sel_rows()) {
		return false;
	}
	const size_t num_hw = pending.hw_slot.size();
	const size_t num_cols = gstate.projected_columns.size();

	// Per hardware column: the interval of the conjuncts the hardware takes, those conjuncts and whether
	// one of them is required by DuckDB, i.e. not an optional join bound. Only a required conjunct saves
	// filter time on the CPU.
	std::vector<std::optional<HwInterval>> intervals(num_hw);
	std::vector<std::vector<size_t>> taken(num_hw);
	std::vector<bool> takes_required(num_hw, false);
	for (size_t k = 0; k < num_hw; k++) {
		const size_t i = pending.hw_slot[k];
		const auto &col = gstate.projected_columns[i];
		const auto &cc = *pending.hw_chunks[k];
		const bool lane32 = col.TransferSize(pending.narrowed[i]) == sizeof(int32_t);

		std::vector<size_t> candidates;
		for (size_t f = 0; f < lstate.scan_filters.size(); f++) {
			const auto &filter = lstate.scan_filters[f];
			if (filter.filter_idx == i && filter.hw_interval && pending.needs_row_filter[f]) {
				candidates.push_back(f);
			}
		}
		if (candidates.empty() || !ChunkHasNoNulls(lstate, pending.group, cc, col.column_id)) {
			continue;
		}
		// One negated interval goes as it is; next to other conjuncts it stays on the CPU, since the
		// hardware takes one interval per column
		std::optional<HwInterval> interval;
		for (size_t f : candidates) {
			const auto &candidate = *lstate.scan_filters[f].hw_interval;
			if (candidate.negate && candidates.size() > 1) {
				continue;
			}
			interval = interval ? Intersect(*interval, candidate) : candidate;
			taken[k].push_back(f);
			takes_required[k] = takes_required[k] || !lstate.scan_filters[f].hw_optional;
		}
		if (interval) {
			intervals[k] = FitToLane(*interval, lane32);
		}
	}

	// A column is emitted when the plan consumes it or a conjunct on it stays on the CPU.
	auto has_cpu_conjunct = [&](size_t k) {
		const size_t i = pending.hw_slot[k];
		for (size_t f = 0; f < lstate.scan_filters.size(); f++) {
			if (lstate.scan_filters[f].filter_idx == i && pending.needs_row_filter[f] &&
			    std::find(taken[k].begin(), taken[k].end(), f) == taken[k].end()) {
				return true;
			}
		}
		return false;
	};
	std::vector<bool> hw_emit(num_cols, false);
	for (size_t k = 0; k < num_hw; k++) {
		const size_t i = pending.hw_slot[k];
		hw_emit[i] = gstate.column_emitted[i] || has_cpu_conjunct(k);
	}

	// The hardware compares one emitted column at most: its values leave compacted by the final
	// selection only if it is the last filter column. The first emitted filter column with a required
	// conjunct is compared, else the first one. The other emitted filter columns keep their conjuncts
	// on the CPU, which then filters the compacted vectors.
	std::optional<size_t> compare_emitted;
	for (size_t k = 0; k < num_hw; k++) {
		if (intervals[k] && hw_emit[pending.hw_slot[k]] &&
		    (!compare_emitted || (takes_required[k] && !takes_required[*compare_emitted]))) {
			compare_emitted = k;
		}
	}
	bool any_filter = false;
	bool any_required = false;
	for (size_t k = 0; k < num_hw; k++) {
		if (!intervals[k]) {
			continue;
		}
		if (hw_emit[pending.hw_slot[k]] && k != *compare_emitted) {
			intervals[k].reset();
			taken[k].clear();
			continue;
		}
		any_filter = true;
		any_required = any_required || takes_required[k];
	}
	if (!any_filter) {
		return false;
	}

	// Take the group when the statistics expect at most the share of passing rows at which DuckDB
	// still evaluates a min/max filter.
	if (!any_required) {
		double pass_fraction = 1;
		for (size_t k = 0; k < num_hw; k++) {
			if (intervals[k]) {
				const size_t column_id = gstate.projected_columns[pending.hw_slot[k]].column_id;
				pass_fraction =
				    std::min(pass_fraction, EstimatedPassFraction(lstate, pending.group, column_id, *intervals[k]));
			}
		}
		float threshold;
		idx_t n_vectors_to_check;
		GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::MIN_MAX, threshold, n_vectors_to_check);
		if (pass_fraction > threshold) {
			DUCKDB_LOG_DEBUG(context,
			                 "Row group %llu on the CPU filter: the join bounds keep an estimated %.3f of the rows",
			                 (unsigned long long)pending.group, pass_fraction);
			return false;
		}
	}

	// Without any emitted column the last filter column emits to carry the row count
	if (std::none_of(hw_emit.begin(), hw_emit.end(), [](bool emit) { return emit; })) {
		for (size_t k = 0; k < num_hw; k++) {
			if (intervals[k]) {
				compare_emitted = k;
			}
		}
		hw_emit[pending.hw_slot[*compare_emitted]] = true;
	}

	// Every emitted column has to be selectable at lane granularity, else the row counts differ
	for (size_t k = 0; k < num_hw; k++) {
		const size_t i = pending.hw_slot[k];
		if (hw_emit[i] && !HwFilterLaneType(TransferType(bind, gstate.projected_columns[i]))) {
			DUCKDB_LOG_DEBUG(context, "Row group %llu on the CPU filter: column '%s' is emitted but has no lanes",
			                 (unsigned long long)pending.group,
			                 bind.metadata.column_names[gstate.projected_columns[i].column_id].c_str());
			return false;
		}
	}

	// Order: the filter columns that are not emitted, then the emitted filter column, then the rest
	std::vector<size_t> order;
	order.reserve(num_hw);
	for (size_t k = 0; k < num_hw; k++) {
		if (intervals[k] && !hw_emit[pending.hw_slot[k]]) {
			order.push_back(k);
		}
	}
	if (compare_emitted) {
		order.push_back(*compare_emitted);
	}
	for (size_t k = 0; k < num_hw; k++) {
		if (!intervals[k]) {
			order.push_back(k);
		}
	}
	D_ASSERT(order.size() == num_hw);

	// Commit: the specs in dispatch order, the conjuncts the hardware takes
	std::vector<oasis::FilterSpec> specs(num_hw);
	for (size_t k : order) {
		auto &spec = specs[k];
		if (intervals[k]) {
			spec.compare = true;
			spec.negate = intervals[k]->negate;
			spec.lo = intervals[k]->lo;
			spec.hi = intervals[k]->hi;
			spec.store_sel = true;
		}
		spec.load_sel = k != order.front();
		spec.emit = hw_emit[pending.hw_slot[k]];
		for (size_t f : taken[k]) {
			pending.needs_row_filter[f] = false;
		}
		DUCKDB_LOG_DEBUG(context, "Row group %llu: column '%s' %s", (unsigned long long)pending.group,
		                 bind.metadata.column_names[gstate.projected_columns[pending.hw_slot[k]].column_id].c_str(),
		                 SpecText(spec).c_str());
	}

	pending.hw_filtered = true;
	pending.specs = std::move(specs);
	pending.order = std::move(order);
	pending.hw_emit = std::move(hw_emit);
	lstate.hw_filter_groups++;
	lstate.hw_filter_rows_in += pending.num_rows;
	return true;
}

} // namespace duckdb
