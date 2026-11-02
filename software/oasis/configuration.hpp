#pragma once

#include "libstf/common.hpp"
#include "oasis/operator.hpp"
#include <coyote/cThread.hpp>
#include <libstf/configuration.hpp>
#include <parcore/configuration.hpp>

#include <vector>

namespace oasis {

constexpr const uint64_t OASIS_SYSTEM_ID = 0x0A515;

// Per-stream write registers: [0] vaddr, [1] size, [2] ctid.
constexpr const uint64_t READ_REQ_CONFIG_REGS = 3;
constexpr const uint64_t READ_REQ_CONFIG_ID   = 0x2f966a70f04c0e93;

/**
 * Configues a hardware read request module to fetch data.
 */
class ReadReqConfig : public libstf::Config {
  public:
    ReadReqConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                  uint32_t num_regs);

    /**
     * For RDMA reads, sets the base vaddr of the remote region that `stream`'s read addresses are
     * relative to. Each stream has its own queue pair, so the remote region base can differ per
     * stream. It must be set before the first enqueue_read() on that stream.
     */
    void set_base_vaddr(libstf::stream_t stream, uintptr_t base_vaddr);

    /**
     * Set the Coyote thread id the hardware issues `stream`'s reads for. Must be set before the
     * first enqueue_read() on that stream.
     */
    void set_ctid(libstf::stream_t stream, uint32_t ctid);

    /**
     * Triggers a read request using the `RDMARead` or `LocalRead` module.
     *
     * Reads are relative to the base virtual address set via set_base_vaddr().
     */
    void enqueue_read(libstf::stream_t stream, size_t vaddr, size_t size);

    const libstf::stream_t num_streams() const;

    static constexpr size_t MAXIMUM_NUM_ENQUEUED_REQUESTS = 64;

    static constexpr uint64_t ID = READ_REQ_CONFIG_ID;

  private:
    libstf::stream_t       num_streams_;
    std::vector<uintptr_t> base_vaddrs_;
};

// Per-decoder write registers: [0] lo, [1] hi, [2] ctrl. Read registers: [0] ID, [1] number of
// decoders, [2] FIFO depth, [3] rows of the selection bitmap.
constexpr const uint64_t FILTER_CONFIG_REGS = 3;
constexpr const uint64_t FILTER_CONFIG_ID   = 0x14e8418192d46a72;

// The bits of the ctrl register, filter_ctrl_t in hardware/src/hdl/common.sv.
constexpr const uint64_t FILTER_CTRL_EMIT      = 1u << 0;
constexpr const uint64_t FILTER_CTRL_STORE_SEL = 1u << 1;
constexpr const uint64_t FILTER_CTRL_LOAD_SEL  = 1u << 2;
constexpr const uint64_t FILTER_CTRL_NEGATE    = 1u << 3;
constexpr const uint64_t FILTER_CTRL_COMPARE   = 1u << 4;

/**
 * Configures the FilterStage of each decoder: one FilterSpec per column chunk.
 */
class FilterConfig : public libstf::Config {
  public:
    FilterConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                 uint32_t num_regs);

    /**
     * Queues the FilterSpec of the next column chunk of `decoder`. The hardware pairs it with the
     * next column chunk configuration of the decoder, so write exactly one per column chunk, before
     * the column chunk configuration (DecodeColumnChunkOperator::apply does both).
     */
    void enqueue(libstf::stream_t decoder, const FilterSpec &spec);

    const libstf::stream_t num_decoders() const;
    const size_t           maximum_num_enqueued_confs() const;

    /**
     * Rows of the selection bitmap of a decoder: the largest row group whose selection the
     * FilterStage can carry across the columns.
     */
    const size_t sel_rows() const;

    static constexpr uint64_t ID = FILTER_CONFIG_ID;

  private:
    libstf::stream_t num_decoders_;
    size_t           maximum_num_enqueued_confs_;
    size_t           sel_rows_;
};

// GenericConfig read side: local address 0 is the config ID, 1 the OutputWriter notify counter,
// and (RDMA builds only) 2..6 the bypass stream's StreamProfiler counters.
constexpr const uint64_t GENERIC_CONFIG_ID                 = static_cast<uint64_t>(-1);
constexpr const uint32_t GENERIC_CONFIG_NOTIFY_COUNT_REG   = 1;
constexpr const uint32_t GENERIC_CONFIG_BYPASS_PROFILE_REG = 2;
constexpr const uint32_t NUM_BYPASS_PROFILE_REGS           = 5;

class GenericConfig : public libstf::Config {
  public:
    GenericConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                  uint32_t num_regs);

    uint64_t notify_count();

    bool has_bypass_profile() const;

    parcore::StreamProfile read_bypass_profile();

    static constexpr uint64_t ID = GENERIC_CONFIG_ID;
};

} // namespace oasis
