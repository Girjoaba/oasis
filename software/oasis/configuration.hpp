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

// Per-decoder write registers: [0] lo, [1] hi, [2] ctrl, [3] lut_min; after them [0] table_addr,
// [1] table_data. Read registers: [0] ID, [1] number of decoders, [2] FIFO depth, [3] rows of the
// selection bitmap, [4] bits of the lookup table. A bitstream without lookup table has only lo, hi
// and ctrl per decoder and the read registers 0 to 3.
constexpr const uint64_t FILTER_CONFIG_REGS           = 4;
constexpr const uint64_t FILTER_CONFIG_REGS_NO_LOOKUP = 3;
constexpr const uint64_t FILTER_TABLE_REGS            = 2;
constexpr const uint64_t FILTER_CONFIG_ID             = 0x14e8418192d46a72;

// The bits of the ctrl register, filter_ctrl_t in hardware/src/hdl/common.sv.
constexpr const uint64_t FILTER_CTRL_EMIT        = 1u << 0;
constexpr const uint64_t FILTER_CTRL_STORE_SEL   = 1u << 1;
constexpr const uint64_t FILTER_CTRL_LOAD_SEL    = 1u << 2;
constexpr const uint64_t FILTER_CTRL_NEGATE      = 1u << 3;
constexpr const uint64_t FILTER_CTRL_COMPARE     = 1u << 4;
constexpr const uint64_t FILTER_CTRL_LOOKUP      = 1u << 5;
constexpr const uint32_t FILTER_CTRL_SHIFT_BIT   = 6;  // 6 bits
constexpr const uint32_t FILTER_CTRL_BASE_BIT    = 16; // 24 bits
constexpr const uint32_t FILTER_LOOKUP_MAX_SHIFT = 31;

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
     * the column chunk configuration (DecodeColumnChunkOperator::apply does both). The first spec
     * that looks up a region writes its words into the tables first (write_table). Call it from
     * one thread only, the dispatcher thread.
     */
    void enqueue(libstf::stream_t decoder, const FilterSpec &spec);

    const libstf::stream_t num_decoders() const;
    const size_t           maximum_num_enqueued_confs() const;

    /**
     * Rows of the selection bitmap of a decoder: the largest row group whose selection the
     * FilterStage can carry across the columns.
     */
    const size_t sel_rows() const;

    /**
     * Bits of the lookup table of a decoder, 0 without lookup.
     */
    const size_t table_bits() const;

    /**
     * Writes `words` into the lookup tables of all decoders, from word `base` on. The writes take
     * effect before every FilterSpec enqueued after them, so a chunk that looks the words up has to
     * be enqueued after this call, and no chunk in flight may look up the old words. Only the words
     * that differ from the last ones written there go to the hardware, one run of words per address
     * write, so a repeated query writes its bitmap once.
     */
    void write_table(size_t base, const std::vector<uint64_t> &words);

    static constexpr uint64_t ID = FILTER_CONFIG_ID;

  private:
    libstf::stream_t num_decoders_;
    size_t           maximum_num_enqueued_confs_;
    size_t           sel_rows_;
    uint64_t         regs_per_decoder_;
    size_t           table_bits_;

    // The words of the tables as last written by write_table. A word is unknown until then: the
    // tables keep their contents from an earlier process and are not cleared.
    std::vector<uint64_t> table_words_;
    std::vector<bool>     table_known_;
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
