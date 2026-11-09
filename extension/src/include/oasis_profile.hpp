#pragma once

#include "duckdb.hpp"

namespace duckdb {

// Registers the `oasis_stream_profile` table function, which reads the per-decoder StreamProfiler
// counters out of the hardware and reports both the raw counters and derived throughput numbers,
// plus the software counters of each stream: post_filter_bytes (bytes the hardware wrote to host
// memory, after the FilterStage) and no_buffer_ns (time without a host buffer while the stream's
// capability had work). The software counters count since the Oasis context opened.
void RegisterOasisProfileFunction(ExtensionLoader &loader);

} // namespace duckdb
