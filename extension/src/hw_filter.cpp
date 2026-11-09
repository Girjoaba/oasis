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
#include "filter_pushdown.hpp"
#include "hw_bitmap.hpp"
#include "oasis/configuration.hpp"
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

std::optional<int64_t> StoredConstant(const Expression &constant, const LogicalType &scan_type) {
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

// What the hardware takes of a scan filter on a hardware column. The hardware
// takes a comparison of the column with a constant: a constant filter of the query, the one-key
// equality of a hash join, and the min/max bounds of a hash join, which come as an optional or
// selectivity-optional filter around such a comparison. It looks the values up in a bitmap for the
// prefix range filter of a hash join (selectivity-optional) and for an IN list of constants. DuckDB
// pushes an IN list that is not a dense range as an optional filter, for the statistics, and keeps
// the IN itself above the scan (the constant IN of the query) or checks the rows in the join (the IN
// list of a hash join), so the hardware may pass a superset: both bitmaps fold when their span does
// not fit. It looks the values up in a Bloom filter for the Bloom filter of a hash join
// (selectivity-optional), folded to the table. Everything else stays on the CPU: the dynamic
// (top-N) filters, OR, LIKE, a type the hardware does not compare and a column a pushed cast
// narrowed. Independent of the row group. The stage limits what the hardware takes: 1 the
// comparisons of the plan, 2 also those of the joins (`dynamic`), 3 also the bitmaps, 5 also the
// Bloom filters (`bloom`, with a bitstream that takes them). It also notes whether the hardware passes
// a superset, whether DuckDB checks the rows and the threshold of a join filter (HwScanFilter).
static HwScanFilter ClassifyHardwareFilter(ClientContext &context, oasis::FilterTable &table,
                                           const OasisScanFilter &filter, const ProjectedColumn &col,
                                           const LogicalType &scan_type, libstf::type_t transfer_type,
                                           uint64_t stage, bool dynamic, bool bloom) {
	HwScanFilter hw;
	// A column a cast narrowed, or a filter rebound to the narrow type, keeps its filters on the CPU
	if (col.cast_type || filter.row_filter || !filter.prune_on_stats) {
		return hw;
	}
	if (dynamic && stage < 2) {
		return hw;
	}
	if (!HwFilterCompareType(transfer_type)) {
		return hw;
	}
	if (filter.filter.filter_type != TableFilterType::EXPRESSION_FILTER) {
		return hw;
	}
	auto &expr_filter = filter.filter.Cast<ExpressionFilter>();
	if (!expr_filter.column_indexes.empty() || !expr_filter.expr) {
		return hw; // Multi-column filter
	}
	const auto &expr = *expr_filter.expr;
	auto classify = [&](const Expression &e, bool optional) {
		hw.interval = ConstantInterval(e, scan_type);
		if (hw.interval || stage < 3) {
			return;
		}
		if (auto bitmap = LookupBitmap(context, table, e, scan_type, optional, bloom)) {
			hw.interval = HwInterval {bitmap->min, bitmap->max, false};
			hw.bitmap = bitmap->words.empty() ? nullptr : bitmap;
			hw.int32_key = ReadsInt32Key(e, scan_type);
			hw.superset = !bitmap->exact;
		}
	};
	// The min/max bounds, the prefix range bitmap, the Bloom filter and the IN lists of a join, and
	// the IN list of the query: optional, so the hardware may drop the rows outside or keep them.
	auto join_bound = ExpressionFilter::GetOptionalFilterChild(expr);
	if (join_bound) {
		classify(*join_bound, true);
		hw.optional = hw.interval.has_value();
		// DuckDB checks the rows of a selectivity-optional filter while it keeps at most its threshold
		// of them, and the rows of an optional filter never
		auto &wrapper = expr.Cast<BoundFunctionExpression>();
		if (wrapper.Function().GetName() == SelectivityOptionalFilterScalarFun::NAME) {
			hw.threshold = wrapper.BindInfo()->Cast<SelectivityOptionalFilterFunctionData>().selectivity_threshold;
		} else {
			idx_t n_vectors_to_check;
			GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::MIN_MAX, hw.threshold, n_vectors_to_check);
			hw.rows_checked = false;
		}
		return hw;
	}
	classify(expr, false);
	return hw;
}

// The type the decoder emits for the column before any narrowing of a chunk.
static libstf::type_t TransferType(const OasisScanBindData &bind, const ProjectedColumn &col) {
	if (col.dict_ids) {
		return libstf::type_t::INT32_T;
	}
	return col.cast_type.value_or(bind.metadata.groups[0].chunks[col.column_id].logical_type());
}

std::vector<HwScanFilter> ClassifyHardwareFilters(ClientContext &context, const OasisScanGlobalState &gstate,
                                                  const OasisScanBindData &bind) {
	std::vector<OasisScanFilter> scan_filters, static_filters;
	BuildScanFilters(context, *gstate.filters, gstate, bind, scan_filters);
	if (gstate.static_filters) {
		BuildScanFilters(context, *gstate.static_filters, gstate, bind, static_filters);
	}
	// A conjunct of the plan, else one the dynamic filter merge added
	auto is_static = [&](const OasisScanFilter &filter) {
		if (filter.filter.filter_type != TableFilterType::EXPRESSION_FILTER) {
			return true;
		}
		return std::any_of(static_filters.begin(), static_filters.end(), [&](const OasisScanFilter &other) {
			return other.filter_idx == filter.filter_idx && other.filter.filter_type == TableFilterType::EXPRESSION_FILTER &&
			       filter.filter.Cast<ExpressionFilter>().Equals(other.filter.Cast<ExpressionFilter>());
		});
	};
	const bool bloom = gstate.hw_filter_stage >= 5 && gstate.ctx->filter_bloom();
	std::vector<HwScanFilter> hw_filters(scan_filters.size());
	for (size_t f = 0; f < scan_filters.size(); f++) {
		const auto &col = gstate.projected_columns[scan_filters[f].filter_idx];
		// The filters of a dictionary id column are evaluated over its dictionary per row group (hw_dictionary.hpp)
		if (col.is_cpu || col.dict_ids) {
			continue;
		}
		const auto &scan_type = bind.output_types[col.column_id];
		auto &hw = hw_filters[f];
		hw = ClassifyHardwareFilter(context, gstate.ctx->filter_table(), scan_filters[f], col, scan_type,
		                            TransferType(bind, col), gstate.hw_filter_stage, !is_static(scan_filters[f]), bloom);
		if (hw.bitmap) {
			const auto &bitmap = *hw.bitmap;
			const auto &name = bind.metadata.column_names[col.column_id];
			if (bitmap.bloom) {
				DUCKDB_LOG_DEBUG(context,
				                 "Hardware filter on column '%s': optional bloom [%lld, %lld], %llu words, %.0f keys, %.4f pass",
				                 name.c_str(), (long long)bitmap.min, (long long)bitmap.max,
				                 (unsigned long long)bitmap.words.size(), bitmap.bloom_keys, bitmap.bloom_pass);
			} else {
				DUCKDB_LOG_DEBUG(context, "Hardware filter on column '%s': %sbitmap [%lld, %lld] >> %u, %llu words, %.3f set",
				                 name.c_str(), hw.optional ? "optional " : "", (long long)bitmap.min,
				                 (long long)bitmap.max, bitmap.shift, (unsigned long long)bitmap.words.size(),
				                 bitmap.OnesFraction(bitmap.min, bitmap.max));
			}
		} else if (hw.interval) {
			DUCKDB_LOG_DEBUG(context, "Hardware filter on column '%s': %s%s[%lld, %lld]",
			                 bind.metadata.column_names[col.column_id].c_str(), hw.optional ? "join " : "",
			                 hw.interval->negate ? "not " : "", (long long)hw.interval->lo, (long long)hw.interval->hi);
		}
		if (hw.int32_key) {
			DUCKDB_LOG_DEBUG(context,
			                 "Hardware filter on column '%s': INTEGER join key, only in row groups whose values fit",
			                 bind.metadata.column_names[col.column_id].c_str());
		}
	}
	return hw_filters;
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

// Whether the interval passes every value of the lane.
static bool PassesLane(const HwInterval &interval, bool lane32) {
	const auto fit = FitToLane(interval, lane32);
	const int64_t lo = lane32 ? NumericLimits<int32_t>::Minimum() : NumericLimits<int64_t>::Minimum();
	const int64_t hi = lane32 ? NumericLimits<int32_t>::Maximum() : NumericLimits<int64_t>::Maximum();
	return !fit.negate && fit.lo <= lo && fit.hi >= hi;
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

// Whether the chunk statistics prove that every value of a BIGINT chunk is an INTEGER key of a
// compressed join (HwScanFilter::int32_key) that the hardware takes as DuckDB does: the cast is then
// the identity. A Bloom filter in a 64 bit lane also needs min >= 0, since the hardware hashes the
// 64 bit value and DuckDB the 32 bit zero extension of the key; a narrowed chunk hashes that without
// hash64.
static bool ChunkFitsInt32Key(OasisScanLocalState &lstate, size_t group, size_t column_id, bool bloom64) {
	auto stats = ChunkStats(lstate, group, column_id);
	if (!stats || !NumericStats::HasMinMax(*stats)) {
		return false;
	}
	const int64_t lo = bloom64 ? 0 : NumericLimits<int32_t>::Minimum();
	return NumericStats::GetMin<int64_t>(*stats) >= lo &&
	       NumericStats::GetMax<int64_t>(*stats) <= NumericLimits<int32_t>::Maximum();
}

// The share of the chunk's rows that pass the interval and the bitmap if the values spread evenly
// over the [min, max] of the chunk statistics; 1 without statistics, or the share of the values of
// the interval that the bitmap passes. The keys of a Bloom filter spread evenly over the interval,
// which holds the min/max bounds of its join when the hardware takes them too.
static double EstimatedPassFraction(OasisScanLocalState &lstate, size_t group, size_t column_id,
                                    const HwInterval &interval, const HwBitmap *bitmap) {
	auto bitmap_fraction = [&](const HwInterval &range) {
		if (bitmap && bitmap->bloom) {
			return bitmap->PassFraction(interval.lo, interval.hi);
		}
		return bitmap && range.lo <= range.hi ? bitmap->PassFraction(range.lo, range.hi) : 1;
	};
	auto stats = ChunkStats(lstate, group, column_id);
	if (!stats || !NumericStats::HasMinMax(*stats)) {
		return bitmap_fraction(interval);
	}
	auto min = StoredValue(NumericStats::Min(*stats));
	auto max = StoredValue(NumericStats::Max(*stats));
	if (!min || !max || *min > *max) {
		return bitmap_fraction(interval);
	}
	const auto overlap = Intersect(HwInterval {interval.lo, interval.hi, false}, HwInterval {*min, *max, false});
	const long double inside = overlap.lo <= overlap.hi
	                               ? ((long double)overlap.hi - overlap.lo + 1) / ((long double)*max - *min + 1)
	                               : 0;
	if (interval.negate) {
		return double(1 - inside);
	}
	return double(inside) * bitmap_fraction(overlap);
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
	std::vector<std::shared_ptr<const HwBitmap>> bitmaps(num_hw);
	// Whether the bitmap of the column has INTEGER keys of a BIGINT column (HwScanFilter::int32_key)
	std::vector<bool> int32_keys(num_hw, false);
	std::vector<std::vector<size_t>> taken(num_hw);
	std::vector<bool> takes_required(num_hw, false);
	for (size_t k = 0; k < num_hw; k++) {
		const size_t i = pending.hw_slot[k];
		const auto &col = gstate.projected_columns[i];
		const auto &cc = *pending.hw_chunks[k];
		const bool lane32 = col.TransferSize(pending.narrowed[i]) == sizeof(int32_t);

		// The ids that pass the conjuncts of a dictionary id column, all of them at once. Its bitmap is exact,
		// so without room in the table the conjuncts stay on the CPU.
		if (col.dict_ids) {
			const auto &id_filter = pending.dict_id_filters[i];
			if (!id_filter) {
				continue;
			}
			if (id_filter->hw.bitmap &&
			    !id_filter->hw.bitmap->Region(context, gstate.ctx->filter_table(), gstate.table_counters)) {
				gstate.table_counters.cpu_fallbacks++;
				continue;
			}
			intervals[k] = id_filter->hw.interval;
			bitmaps[k] = id_filter->hw.bitmap;
			taken[k] = id_filter->conjuncts;
			takes_required[k] = id_filter->required;
			continue;
		}

		std::vector<size_t> candidates;
		for (size_t f = 0; f < lstate.scan_filters.size(); f++) {
			const auto &filter = lstate.scan_filters[f];
			if (filter.filter_idx == i && filter.hw.interval && pending.needs_row_filter[f]) {
				candidates.push_back(f);
			}
		}
		if (candidates.empty() || !ChunkHasNoNulls(lstate, pending.group, cc, col.column_id)) {
			continue;
		}
		// One negated interval goes as it is; next to other conjuncts it stays on the CPU, since the
		// hardware takes one interval per column. Likewise one bitmap or Bloom filter per column; a
		// 32 bit lane takes the difference to the min of a bitmap in 32 bits, so its span has to fit.
		// The bitmap of a required conjunct needs its region now, else the conjunct stays on the CPU;
		// so does the Bloom filter of a join, whose interval alone (the range of the type) passes every
		// value. The Bloom filter of the keys of a prefix range filter keeps its [min, max], as a bitmap.
		std::optional<HwInterval> interval;
		for (size_t f : candidates) {
			const auto &hw = lstate.scan_filters[f].hw;
			const auto &candidate = *hw.interval;
			if (candidate.negate && candidates.size() > 1) {
				continue;
			}
			// The INTEGER key of a compressed join only where the cast is the identity; its values then
			// lie in the 32 bit range, whose interval passes the lane
			if (hw.int32_key &&
			    !ChunkFitsInt32Key(lstate, pending.group, col.column_id, hw.bitmap && hw.bitmap->bloom && !lane32)) {
				DUCKDB_LOG_DEBUG(context,
				                 "Row group %llu on the CPU filter for column '%s': the INTEGER join key does not fit",
				                 (unsigned long long)pending.group, bind.metadata.column_names[col.column_id].c_str());
				continue;
			}
			if (hw.bitmap && (bitmaps[k] || (lane32 && !hw.bitmap->bloom &&
			                                 uint64_t(hw.bitmap->max) - uint64_t(hw.bitmap->min) > UINT32_MAX))) {
				continue;
			}
			if (hw.bitmap && (!hw.optional || (hw.bitmap->bloom && PassesLane(candidate, lane32 || hw.int32_key))) &&
			    !hw.bitmap->Region(context, gstate.ctx->filter_table(), gstate.table_counters)) {
				gstate.table_counters.cpu_fallbacks++;
				continue;
			}
			interval = interval ? Intersect(*interval, candidate) : candidate;
			if (hw.bitmap) {
				bitmaps[k] = hw.bitmap;
				int32_keys[k] = hw.int32_key;
			}
			taken[k].push_back(f);
			takes_required[k] = takes_required[k] || !hw.optional;
		}
		if (interval) {
			intervals[k] = FitToLane(*interval, lane32);
			// No value passes, so no lookup
			if (intervals[k]->lo > intervals[k]->hi) {
				bitmaps[k].reset();
			}
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
			bitmaps[k].reset();
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
	// still evaluates the join filter (HwScanFilter::threshold): the interval against the largest
	// threshold of the conjuncts without a bitmap, the bitmap against the threshold of its prefix range
	// or Bloom filter. The bitmap counts only with its region, else the interval goes alone.
	if (!any_required) {
		float min_max_threshold;
		idx_t n_vectors_to_check;
		GetThresholdAndVectorsToCheck(SelectivityOptionalFilterType::MIN_MAX, min_max_threshold, n_vectors_to_check);
		double pass_fraction = 1;
		bool selective = false;
		for (size_t k = 0; k < num_hw; k++) {
			if (!intervals[k]) {
				continue;
			}
			const size_t i = pending.hw_slot[k];
			if (gstate.projected_columns[i].dict_ids) {
				pass_fraction = std::min(pass_fraction, pending.dict_id_filters[i]->pass_fraction);
				selective = selective || pending.dict_id_filters[i]->pass_fraction <= min_max_threshold;
				continue;
			}
			float interval_threshold = 0;
			float bitmap_threshold = 0;
			for (size_t f : taken[k]) {
				const auto &hw = lstate.scan_filters[f].hw;
				auto &threshold = hw.bitmap ? bitmap_threshold : interval_threshold;
				threshold = std::max(threshold, hw.threshold);
			}
			const size_t column_id = gstate.projected_columns[i].column_id;
			const double interval_fraction =
			    EstimatedPassFraction(lstate, pending.group, column_id, *intervals[k], nullptr);
			pass_fraction = std::min(pass_fraction, interval_fraction);
			selective = selective || interval_fraction <= interval_threshold;
			if (bitmaps[k]) {
				const double bitmap_fraction =
				    EstimatedPassFraction(lstate, pending.group, column_id, *intervals[k], bitmaps[k].get());
				pass_fraction = std::min(pass_fraction, bitmap_fraction);
				selective = selective ||
				            (bitmap_fraction <= bitmap_threshold &&
				             bitmaps[k]->Region(context, gstate.ctx->filter_table(), gstate.table_counters));
			}
		}
		if (!selective) {
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
		// An optional bitmap without room in the table leaves the interval alone, a superset
		if (bitmaps[k]) {
			spec.lookup = bitmaps[k]->Region(context, gstate.ctx->filter_table(), gstate.table_counters);
			if (!spec.lookup) {
				gstate.table_counters.interval_fallbacks++;
			}
		}
		if (spec.lookup && bitmaps[k]->bloom) {
			// The hash of DuckDB: of the 64 bit key of a BIGINT column also in a chunk narrowed to 32 bit lanes,
			// of the 32 bit key of an INTEGER join key
			const auto &col = gstate.projected_columns[pending.hw_slot[k]];
			spec.bloom = true;
			spec.hash64 = bind.output_types[col.column_id].InternalType() == PhysicalType::INT64 && !int32_keys[k];
		} else if (spec.lookup) {
			spec.lut_min = bitmaps[k]->min;
			spec.shift = bitmaps[k]->shift;
		}
		spec.load_sel = k != order.front();
		spec.emit = hw_emit[pending.hw_slot[k]];
		// A conjunct the hardware passes a superset of keeps DuckDB's check on the CPU, which filters
		// the compacted vectors, if DuckDB checks its rows and the column is emitted anyway: a bitmap
		// that is not exact, also as its interval alone, or a bitmap without its region
		const bool dict_ids = gstate.projected_columns[pending.hw_slot[k]].dict_ids;
		for (size_t f : taken[k]) {
			const auto &hw = lstate.scan_filters[f].hw;
			const bool superset = hw.superset || (hw.bitmap && bitmaps[k] && !spec.lookup);
			if (superset && hw.rows_checked && spec.emit && !dict_ids) {
				DUCKDB_LOG_DEBUG(context, "Row group %llu: column '%s' also on the CPU, the hardware passes a superset",
				                 (unsigned long long)pending.group,
				                 bind.metadata.column_names[gstate.projected_columns[pending.hw_slot[k]].column_id].c_str());
				continue;
			}
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
