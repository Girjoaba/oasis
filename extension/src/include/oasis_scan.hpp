#pragma once

#include "coalesced_fetcher.hpp"
#include "column_reader.hpp"
#include "duckdb.hpp"
#include "libstf_buffer_vector_buffer.hpp"
#include "oasis/oasis_context.hpp"
#include "oasis/splinter_result.hpp"
#include "oasis_context_cache_entry.hpp"
#include "parcore/metadata/metadata.hpp"
#include "parquet_reader.hpp"

#include "duckdb/parallel/async_result.hpp"
#include "duckdb/planner/table_filter_state.hpp"

#include <atomic>
#include <chrono>
#include <deque>
#include <mutex>
#include <optional>

namespace duckdb {

class PhysicalTableScan;

// The last stage of the hardware filter (oasis_hw_filter_stage), its default.
static constexpr uint64_t HW_FILTER_MAX_STAGE = 5;

// Adds the lifetime of the scope to `target_ns`.
class ScopedTimer {
public:
	explicit ScopedTimer(uint64_t &target_ns) : target_ns(target_ns), start(std::chrono::steady_clock::now()) {
	}
	~ScopedTimer() {
		auto elapsed = std::chrono::steady_clock::now() - start;
		target_ns += static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count());
	}

private:
	uint64_t &target_ns;
	std::chrono::steady_clock::time_point start;
};

// Closed interval on the integer the hardware compares (the stored value: days for DATE, the
// unscaled value for DECIMAL, the stored units for TIMESTAMP). With negate the values outside pass.
// See hw_filter.hpp.
struct HwInterval {
	int64_t lo;
	int64_t hi;
	bool negate = false;
};

// What the bitmaps of one scan take of the lookup table of the FilterStages, summed over its workers
// (HwBitmap::Region, PlanHardwareFilter) and reported with the metrics of the scan.
struct HwTableCounters {
	// The regions asked for, one per bitmap at the first row group that looks it up, and those the
	// table had no room for.
	std::atomic<uint64_t> requests {0};
	std::atomic<uint64_t> failures {0};
	// The row groups whose bitmap had no region: an optional bitmap went as its interval alone, the
	// conjuncts of a required one (or of the Bloom filter of a join) stayed on the CPU.
	std::atomic<uint64_t> interval_fallbacks {0};
	std::atomic<uint64_t> cpu_fallbacks {0};
	// The words of the regions taken, and the most words of the table in use (every scan and session)
	// after a request of this scan.
	std::atomic<uint64_t> words {0};
	std::atomic<uint64_t> peak_words_in_use {0};
};

// A yes/no bitmap the FilterStage looks the values of a column up in (hw_bitmap.hpp): a value x with
// min <= x <= max passes if bit (x - min) >> shift of `words` is set. The bitmap of a filter that is
// not optional passes the values of the filter and no other; else it may pass a superset, as a folded
// prefix range bitmap of a join, whose rows the join checks again (and the CPU, when the column is
// emitted: HwScanFilter::superset). With bloom, `words` is a Bloom
// filter of 2^k words in place of the bitmap (hw_bloom.hpp), optional only, and shift is unused.
struct HwBitmap {
	int64_t min;
	int64_t max;
	uint32_t shift;
	std::vector<uint64_t> words;
	// Set bits before each word, for OnesFraction.
	std::vector<uint32_t> ones_before;
	bool bloom = false;
	// The share of the values outside the keys that a Bloom filter passes.
	double bloom_pass = 1;
	// The number of keys of a Bloom filter, estimated from its set bits for the filter of a join.
	double bloom_keys = 0;
	// Whether min, max and the words pass the values of the filter and no other: a bitmap of shift 0,
	// or one that sets every bit, and the Bloom filter of a join copied without a fold. Not for a folded
	// bitmap, the Bloom filter of the keys of a prefix range filter and the interval alone of a bitmap
	// that sets about every bit.
	bool exact = false;

	// The share of set bits among the bits of the values lo to hi, which lie in [min, max].
	double OnesFraction(int64_t lo, int64_t hi) const;

	// The share of the values lo to hi that pass: OnesFraction, 1 without words, and for a Bloom filter
	// its keys and bloom_pass of the other values, with the keys spread evenly over lo to hi.
	double PassFraction(int64_t lo, int64_t hi) const;

	// The region of the lookup table that holds the words, taken by the first row group that looks
	// the bitmap up, so that a bitmap no group uses holds no room. Null when the table had no room
	// then, or after ReleaseRegion; every later call returns the same. The request goes into counters
	// and the debug log.
	std::shared_ptr<oasis::FilterTableRegion> Region(ClientContext &context, oasis::FilterTable &table,
	                                                 HwTableCounters &counters) const;

	// Drops the region, which returns to the table when the last flow that looks it up is done (its
	// FilterSpec holds it). The words it held, 0 without a region.
	size_t ReleaseRegion() const;

private:
	mutable std::mutex region_lock;
	mutable bool region_requested = false;
	mutable std::shared_ptr<oasis::FilterTableRegion> region;
};

// What the hardware takes of one scan filter (hw_filter.hpp), the same for every worker and row group.
struct HwScanFilter {
	// The interval the hardware compares for this filter, if it is a comparison of the column with a
	// constant the FilterStage can take.
	std::optional<HwInterval> interval;
	// Whether interval comes from an optional join filter (min/max, prefix range or Bloom filter of a
	// hash join).
	bool optional = false;
	// The bitmap the hardware looks the values up in, with interval its [min, max]: a prefix range
	// filter of a hash join, an IN list or the Bloom filter of a hash join. Null for a comparison.
	std::shared_ptr<const HwBitmap> bitmap;
	// Whether interval and bitmap may pass values the filter drops: a bitmap that is not exact, also as
	// its interval alone. A bitmap without its region also passes a superset (PlanHardwareFilter).
	bool superset = false;
	// Whether DuckDB checks the rows of the filter in the scan: not for an optional filter, which passes
	// every row on the CPU and serves the statistics only.
	bool rows_checked = true;
	// The largest estimated share of passing rows at which the hardware takes an optional filter alone:
	// the threshold above which DuckDB stops checking it (GetThresholdAndVectorsToCheck), 0.9 for the
	// min/max bounds and 0.5 for a prefix range or Bloom filter of a hash join.
	float threshold = 1;
	// Whether the filter reads a BIGINT column as the INTEGER key of a compressed join (ReadsInt32Key of
	// hw_bitmap.hpp): interval and bitmap hold for a chunk only if every value fits, where the cast is the
	// identity, and a Bloom filter hashes INTEGER keys (PlanHardwareFilter).
	bool int32_key = false;
};

// The ids of a dictionary that pass the conjuncts of a dictionary id column (hw_dictionary.hpp), evaluated
// once per row group over the entries of its dictionary.
struct DictIdFilter {
	std::vector<bool> pass;
	// The scan filters evaluated, and whether one of them is required, i.e. not an optional join bound.
	std::vector<size_t> conjuncts;
	bool required = false;
	double pass_fraction = 1;
	// What the hardware takes: an interval of the ids, or their bitmap with the interval [0, size - 1].
	HwScanFilter hw;
};

// The dictionary of a dictionary id column in one row group.
struct GroupDictionary {
	buffer_ptr<DictionaryEntry> entry;
	idx_t size = 0;
};

// Per-worker state for one pushed-down TableFilter. TableFilterState is not safe to share across
// threads, so each worker owns its own (mirrors ParquetReader's per-scan-state scan_filters).
struct OasisScanFilter {
	OasisScanFilter(ClientContext &context, idx_t filter_idx, const TableFilter &filter,
	                unique_ptr<TableFilter> row_filter_p = nullptr)
	    : filter_idx(filter_idx), filter(filter), row_filter(std::move(row_filter_p)),
	      filter_state(TableFilterState::Initialize(context, row_filter ? *row_filter : filter)) {
	}

	// Owning variant: Used for filters synthesized by splitting a pushed-down AND into its
	// conjuncts, so each conjunct is classified and evaluated independently.
	OasisScanFilter(ClientContext &context, idx_t filter_idx, unique_ptr<TableFilter> owned_filter_p,
	                unique_ptr<TableFilter> row_filter_p = nullptr)
	    : filter_idx(filter_idx), owned_filter(std::move(owned_filter_p)), filter(*owned_filter),
	      row_filter(std::move(row_filter_p)),
	      filter_state(TableFilterState::Initialize(context, row_filter ? *row_filter : *owned_filter)) {
	}

	idx_t filter_idx;
	unique_ptr<TableFilter> owned_filter; // null when `filter` references a gstate-owned filter
	const TableFilter &filter;
	// `filter` rebound to the type the scan outputs, when a cast pushed into the scan narrowed the
	// column after the filter was bound to its file type (e.g. a dynamic join filter). Null otherwise.
	// Row-level evaluation uses it, statistics pruning `filter`.
	unique_ptr<TableFilter> row_filter;
	unique_ptr<TableFilterState> filter_state;
	// Whether `filter` takes the file type of the column, the type of the column chunk statistics.
	bool prune_on_stats = true;
	// What the hardware takes of the filter, copied from gstate.hw_filters.
	HwScanFilter hw;
	// On a dictionary id column: the filter evaluated over the dictionary, its child for an optional filter.
	// Null when the filter stays on the rows (dynamic or able to throw).
	unique_ptr<TableFilter> dict_id_filter;
	unique_ptr<TableFilterState> dict_id_filter_state;
	bool dict_id_optional = false;
};

// A string column chunk whose data pages are all dictionary-encoded: the decoders emit its codes, the
// host decodes its dictionary page.
struct DictionaryChunk {
	// The data pages, read from the first one on and decoded as INT32 codes
	parcore::metadata::ColumnChunk ids;
	uint64_t dict_offset;
	uint64_t dict_size;
};

struct OasisScanBindData : public TableFunctionData {
	// DuckDB type of every file column, as read_parquet would report it.
	vector<LogicalType> column_types;
	// DuckDB type the scan returns per file column: column_types, or the narrower type of a cast
	// pushed into the scan (oasis_cast_pushdown).
	vector<LogicalType> output_types;
	string filename;
	parcore::metadata::Metadata metadata;
	shared_ptr<ParquetFileMetadataCache> parquet_metadata;
	// Reader over parquet_metadata that does not open the file. It answers statistics questions at
	// plan time, and must outlive the partition statistics it hands out (they reference it).
	shared_ptr<ParquetReader> metadata_reader;
	// Per row group and file column: the chunk of a string column whose data pages are all
	// dictionary-encoded, as its codes (BuildDictionaryChunks).
	vector<vector<std::optional<DictionaryChunk>>> dict_chunks;
};

struct ProjectedColumn {
	size_t column_id;
	size_t elem_size; // 0 for CPU (string) columns
	bool is_cpu;      // variable-length column decoded on the CPU path, not the FPGA
	// Signed 64-bit integer column (BIGINT or DECIMAL on INT64) whose chunks may be transferred as
	// INT32 when their statistics fit (see oasis_narrow_transfer). The scan widens them back.
	bool narrowable = false;
	// Type the decoders cast the column to for a cast pushed into the scan (oasis_cast_pushdown).
	std::optional<libstf::type_t> cast_type;
	// String column whose chunks go to the hardware as their dictionary ids (elem_size 4).
	bool dict_ids = false;

	// Bytes per value the hardware writes for a chunk of the column, 4 for a narrowed chunk.
	size_t TransferSize(bool narrowed) const {
		return narrowed ? sizeof(int32_t) : elem_size;
	}
};

// Global scan state shared across all DuckDB worker threads of one read_oasis scan. Following
// DuckDB's own Parquet reader, the only shared mutable state is the row-group cursor (an atomic
// each worker claims a group from). Everything per-row-group lives in the local state so workers
// never race on it.
struct OasisScanGlobalState : public GlobalTableFunctionState {
	string filename;
	oasis::OasisContext *ctx = nullptr;

	vector<ProjectedColumn> projected_columns;
	bool has_cpu_columns = false;
	bool has_dict_id_columns = false;

	// Whether chunks of narrowable columns are decoded to INT32 when their statistics fit
	// (oasis_narrow_transfer).
	bool narrow_transfer = false;

	// filter_prune support. When the plan consumes fewer columns than the scan reads (some are
	// filter-only), can_prune is set, projection_ids holds the indices (into projected_columns)
	// of the consumed columns in output order, and column_emitted marks which projected columns
	// are consumed at all. Without pruning, column_emitted is all-true and output == scan layout.
	vector<column_t> projection_ids;
	bool can_prune = false;
	std::vector<bool> column_emitted;

	optional_ptr<TableFilterSet> filters;
	// The filters of the plan, before the dynamic join filters merge into `filters`.
	optional_ptr<TableFilterSet> static_filters;

	// Hardware filtering (oasis_hw_filter): the FilterStage of each decoder compares the values of a
	// column chunk with an interval and drops the rows that fail from every column of the row group
	// (hw_filter.hpp). Off for scans with CPU columns, which would need the selection on the host,
	// and without the FilterStage in the bitstream.
	bool hw_filter = false;
	// The stages the hardware filter takes (oasis_hw_filter_stage): 1 the constant comparisons, 2 the
	// bounds of the joins, 3 the bitmaps, 4 the string columns as their dictionary ids, 5 the Bloom filters.
	uint64_t hw_filter_stage = 0;

	// Whether the decoded dictionaries of the dictionary id columns go to DuckDB's object cache, shared by
	// the scans of every connection (oasis_dict_cache, hw_dictionary.hpp).
	bool dict_cache = false;

	// Dynamic join filters (min/max pushed by hash joins into the probe-side scan). Our global
	// state is created at schedule time (INITIALIZE_ON_SCHEDULE), before the build side has run,
	// so PhysicalTableScan's own dynamic-filter merge sees an empty set and drops them. Instead we
	// keep a pointer to the physical scan and merge its dynamic filters into `filters` once, when
	// the first worker initializes -- the probe pipeline only executes after the build completed.
	optional_ptr<const PhysicalTableScan> physical_scan;
	std::once_flag dynamic_filter_merge;
	unique_ptr<TableFilterSet> merged_filters;

	// What the hardware takes of each filter, one entry per filter of BuildScanFilters, in its order:
	// classified once, after the dynamic filter merge (ClassifyHardwareFilters). The regions of the
	// bitmaps return to the lookup table when DuckDB frees this state, at the end of the query (it
	// never calls Pipeline::ClearSource), and their flows are done; with table_early_release when the
	// scan is done (CloseGroup).
	std::vector<HwScanFilter> hw_filters;

	// oasis_filter_table_early_release: the regions of hw_filters return to the lookup table once the
	// last row group of the scan is collected, so that a later scan of the query finds the room.
	bool table_early_release = false;
	// What the bitmaps of the scan take of the lookup table. Mutable: PlanHardwareFilter and the
	// classification take the state const.
	mutable HwTableCounters table_counters;

	// Row-group cursor: the next group to hand out. Claimed atomically by workers.
	std::atomic<size_t> next_group {0};
	size_t total_groups = 0;
	// The row groups claimed and not yet collected, with the claim in progress of each worker: when it
	// drops to 0 after the cursor ran out, no group of the scan looks a region up any more.
	std::atomic<size_t> groups_open {0};
	std::once_flag scan_done;

	size_t groups_in_flight_per_worker = 1;

	// SET oasis_prefetch_depth = 0: one group at a time per worker, collected by blocking on the
	// worker thread instead of descheduling through the async-task path.
	bool synchronous = false;

	// Cold-start yielding (see the yield-budget registration in OasisScanInitGlobal); disabled via
	// SET oasis_enable_yield = false.
	bool yield_enabled = true;

	// Scan-wide profiling totals, folded in from each worker's local counters when its scan
	// finishes (OasisScanGetMetrics) and reported on the query profiling tree.
	std::atomic<uint64_t> filter_time_ns {0};
	std::atomic<uint64_t> string_decode_time_ns {0};
	std::atomic<uint64_t> hw_filter_groups {0};
	std::atomic<uint64_t> hw_filter_rows_in {0};
	std::atomic<uint64_t> hw_filter_rows_out {0};
	std::atomic<uint64_t> hw_bytes_out {0};
	std::atomic<uint64_t> dict_pages_fetched {0};
	std::atomic<uint64_t> dict_pages_cached {0};

	idx_t MaxThreads() const override {
		return total_groups == 0 ? 1 : total_groups;
	}
};

// Per-worker scan state. Owns this worker's file handle (DuckDB FileHandles are not safe to share
// across threads) and, for the row group it is currently scanning, the decoded buffer per column it
// is slicing into vectors.
//
// One buffer per column chunk: a row group is decoded in full in hardware, each column yielding
// exactly one buffer, and we then slice all columns' buffers in lockstep. The next group is loaded
// only once the current buffers are fully emitted.
struct OasisScanLocalState : public LocalTableFunctionState {
	unique_ptr<FileHandle> file_handle;

	unique_ptr<ParquetReader> parquet_reader;
	unique_ptr<ParquetReaderScanState> scan_state;

	// Per-worker pushed-down filter states, built once at scan init from gstate.filters.
	std::vector<OasisScanFilter> scan_filters;

	// Reused selection vector for row-level filtering, so we don't reallocate per scan call.
	SelectionVector filter_sel;

	// When can_prune, slices are scanned and filtered in this full-width chunk and only the
	// projection_ids columns are referenced into the output chunk.
	DataChunk all_columns;

	// Per-slice scratch: which CPU columns have consumed the current slice from their reader.
	std::vector<bool> cpu_column_read;

	struct PendingGroup {
		size_t group = 0;
		size_t num_rows = 0;

		// Per-scan_filters entry: whether the filter still needs row-level evaluation on this
		// group (false when the group's statistics prove it always-true).
		std::vector<bool> needs_row_filter;

		std::unique_ptr<CoalescedFetcher> fetcher;

		// Hardware columns of this group:
		// hw_slot[k] is the projection index, hw_chunks[k] the chunk, host_handles[k] the fetcher
		// handle resolving hw_chunks[k]'s bytes (host path only).
		std::vector<size_t> hw_slot;
		std::vector<const parcore::metadata::ColumnChunk *> hw_chunks;
		std::vector<CoalescedFetcher::RangeHandle> host_handles;

		// Per projection index: whether the column's chunk of this group is transferred as INT32.
		std::vector<bool> narrowed;

		// CPU (string) columns of this group: Fetched over RDMA as raw bypass flows: cpu_slot[k]
        // is the projection index, cpu_chunks[k] the chunk.
		std::vector<size_t> cpu_slot;
		std::vector<const parcore::metadata::ColumnChunk *> cpu_chunks;

		// Hardware filtering of this group (PlanHardwareFilter): specs[k] is the FilterSpec of
		// hw_chunks[k], order the k in which the chunks go to the decoder, hw_emit per projection
		// index whether the hardware writes the column. Every emitted column has the rows of the
		// filtered group.
		bool hw_filtered = false;
		std::vector<oasis::FilterSpec> specs;
		std::vector<size_t> order;
		std::vector<bool> hw_emit;

		// Per projection index of a dictionary id column: its dictionary and the ids its conjuncts pass.
		std::vector<GroupDictionary> dictionaries;
		std::vector<std::shared_ptr<const DictIdFilter>> dict_id_filters;

		// The dictionary pages fetched over RDMA (LookupDictionaries): dict_slot the projection indexes,
		// dict_buffers per projection index the page. The group's splinter fetches them as bypass flows,
		// tagged with the number of projected columns plus the projection index. When the conjuncts of a
		// column need its dictionary before the group is submitted, a splinter of their own (dict_result)
		// fetches them, and the group is submitted once they are in (awaiting_dictionaries).
		std::vector<size_t> dict_slot;
		std::vector<std::vector<std::shared_ptr<libstf::Buffer>>> dict_buffers;
		oasis::SplinterResultHandle dict_result;
		size_t dict_batches_remaining = 0;
		bool awaiting_dictionaries = false;

		oasis::SplinterResultHandle result;
		bool submitted = false;
		size_t batches_remaining = 0;

		std::vector<std::shared_ptr<libstf::Buffer>> hw_buffers;
		std::vector<std::vector<std::shared_ptr<libstf::Buffer>>> cpu_buffers;
	};

	std::deque<unique_ptr<PendingGroup>> inflight;

	bool groups_exhausted = false;

	std::vector<std::shared_ptr<libstf::Buffer>> current_buffers;

	// The current group's narrowed flags (see PendingGroup).
	std::vector<bool> current_narrowed;

	// The current group's needs_row_filter mask (see PendingGroup), consumed by DecodeAndFilterSlice.
	std::vector<bool> current_needs_row_filter;

	// The current group's hw_emit flags (see PendingGroup): a hardware column the hardware did not
	// write has no buffer contents and gets no vector.
	std::vector<bool> current_hw_emit;

	// The current group's dictionaries and dictionary id filters (see PendingGroup).
	std::vector<GroupDictionary> current_dictionaries;
	std::vector<std::shared_ptr<const DictIdFilter>> current_dict_id_filters;

	// The file in the keys of the cached dictionaries: its path, size and last modification. Empty when
	// the scan caches no dictionary.
	string dict_cache_file;

	size_t current_buf_offset = 0;
	size_t current_group_num_rows = 0;
	idx_t current_group = 0;

	// This worker's profiling counters (steady_clock nanoseconds), folded into the global totals
	// when the worker's scan finishes.
	uint64_t filter_time_ns = 0;
	uint64_t string_decode_time_ns = 0;
	uint64_t hw_filter_groups = 0;
	uint64_t hw_filter_rows_in = 0;
	uint64_t hw_filter_rows_out = 0;
	// Bytes the hardware wrote for the decoded columns of this worker's groups (after the FilterStage).
	uint64_t hw_bytes_out = 0;
	// Dictionary pages of the dictionary id columns read or fetched, and taken from the cache.
	uint64_t dict_pages_fetched = 0;
	uint64_t dict_pages_cached = 0;
};

void RegisterOasisScanFunction(ExtensionLoader &loader);

} // namespace duckdb
