#define DUCKDB_EXTENSION_MAIN

#include "oasis_extension.hpp"
#include "oasis_profile.hpp"
#include "oasis_scan.hpp"
#include "oasis_context_cache_entry.hpp"
#include "oasis_settings.hpp"
#include "oasis_log_sink.hpp"
#include "rdma_file_system.hpp"
#include "duckdb.hpp"
#include "duckdb/function/scalar_function.hpp"
#include "duckdb/main/config.hpp"
#include "duckdb/main/connection.hpp"
#include "duckdb/main/database.hpp"

#include <coyote/cDefs.hpp>

namespace duckdb {

// Default for oasis_prefetch_depth (see the AddExtensionOption below).
static constexpr uint64_t DEFAULT_PREFETCH_DEPTH = 2;

static void LoadInternal(ExtensionLoader &loader) {
	auto &instance = loader.GetDatabaseInstance();
	auto &config = DBConfig::GetConfig(instance);

	config.AddExtensionOption("oasis_logging_level",
	                          "libSTF/ParCore/OAL log verbosity (TRACE, DEBUG, INFO, WARNING, ERROR, FATAL, OFF). "
	                          "Records also pass through DuckDB's logging_level before reaching duckdb_logs",
	                          LogicalType::VARCHAR, Value(DEFAULT_OASIS_LOG_LEVEL), SetOasisLogLevel);
	libstf::set_log_level(ParseLibstfLogLevel(DEFAULT_OASIS_LOG_LEVEL));

	// Oasis table function
	config.AddExtensionOption("oasis_scheduler_num_streams",
	                          "Number of streams the scheduler drives (0 = all available)", LogicalType::UBIGINT,
	                          Value::UBIGINT(0), SetSchedulerNumStreams);
	config.AddExtensionOption("oasis_scheduler_queue_depth",
	                          "Max splinters in flight per stream (0 = hardware config-FIFO depth)",
	                          LogicalType::UBIGINT, Value::UBIGINT(0), SetSchedulerQueueDepth);
	config.AddExtensionOption(
	    "oasis_prefetch_depth",
	    "Row groups each read_oasis worker thread keeps submitted but not yet collected, overlapping "
	    "submission with collection. 0 executes synchronously: each worker submits one group at a "
	    "time and blocks on the worker thread until it completes.",
	    LogicalType::UBIGINT, Value::UBIGINT(DEFAULT_PREFETCH_DEPTH));
	config.AddExtensionOption(
	    "oasis_enable_yield",
	    "Whether read_oasis workers yield to the DuckDB scheduler while waiting on hardware during "
	    "cold start, letting other workers prime the pipeline breadth-first. Set false to disable "
	    "yielding entirely.",
	    LogicalType::BOOLEAN, Value::BOOLEAN(true));
	config.AddExtensionOption(
	    "oasis_narrow_transfer",
	    "Whether read_oasis decodes BIGINT and DECIMAL column chunks whose statistics fit into INT32 "
	    "as INT32 and widens them on the CPU, halving their transfer from the FPGA. Needs a bitstream "
	    "whose decoders cast INT64 to INT32.",
	    LogicalType::BOOLEAN, Value::BOOLEAN(false));
	config.AddExtensionOption(
	    "oasis_hw_filter",
	    "Whether read_oasis pushes constant comparisons on INTEGER, BIGINT, DATE, DECIMAL and TIMESTAMP "
	    "columns into the decoders, which drop the failing rows of every column of the row group before "
	    "the transfer. Needs a bitstream with the FilterStage. Scans with string columns the decoders do not "
	    "take as dictionary ids and row groups with more rows than the selection bitmap holds are filtered on the CPU.",
	    LogicalType::BOOLEAN, Value::BOOLEAN(true));
	config.AddExtensionOption(
	    "oasis_hw_filter_stage",
	    "The stages of the hardware filter read_oasis uses, each with the ones before: 0 none, 1 the constant "
	    "comparisons of the query, 2 the bounds of the hash joins, 3 the bitmap lookup (prefix range filters, "
	    "IN lists), 4 the dictionary-encoded string columns as their dictionary ids and their filters, 5 the "
	    "Bloom filters of the hash joins and the prefix range filters too wide for the bitmap as Bloom filters "
	    "of their keys. Needs oasis_hw_filter; the dictionary ids (4) need decoders that emit them, the Bloom "
	    "filters (5) a FilterStage that takes Bloom filters.",
	    LogicalType::UBIGINT, Value::UBIGINT(HW_FILTER_MAX_STAGE));
	config.AddExtensionOption(
	    "oasis_cast_pushdown",
	    "Whether read_oasis accepts integer narrowing casts that DuckDB pushes into the scan (e.g. BIGINT "
	    "join keys cast to INTEGER) when the file statistics prove that all values fit, and decodes the "
	    "column directly to the narrow type. BIGINT columns need a bitstream whose decoders cast INT64 "
	    "to INT32.",
	    LogicalType::BOOLEAN, Value::BOOLEAN(false));

	// Oasis scan table function
	RegisterOasisScanFunction(loader);

	// Stream profiler readout table function
	RegisterOasisProfileFunction(loader);

	// RDMA file system
	config.AddExtensionOption(
	    "oasis_rdma_server",
	    "RDMA file server IP address (required; set via `SET oasis_rdma_server = '<ip-address>';`)",
	    LogicalType::VARCHAR, Value(LogicalType::VARCHAR));
	config.AddExtensionOption("oasis_rdma_port", "RDMA file server TCP port (for QP exchange)", LogicalType::UBIGINT,
	                          Value::UBIGINT(static_cast<uint64_t>(coyote::DEF_PORT)));

	FileSystem::GetFileSystem(instance).RegisterSubSystem(make_uniq<RDMAFileSystem>());

	// Get the OasisContext to establish the connection to the FPGA.
	Connection conn(instance);
	GetOrCreateOasisContext(*conn.context);
}

void OasisExtension::Load(ExtensionLoader &loader) {
	LoadInternal(loader);
}

std::string OasisExtension::Name() {
	return "Oasis";
}

std::string OasisExtension::Version() const {
#ifdef EXT_VERSION_OASIS
	return EXT_VERSION_OASIS;
#else
	return "";
#endif
}

} // namespace duckdb

extern "C" {
DUCKDB_CPP_EXTENSION_ENTRY(oasis, loader) {
	duckdb::LoadInternal(loader);
}
}
