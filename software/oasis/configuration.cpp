#include <cstring>
#include <stdexcept>
#include <string>

#include <oasis/configuration.hpp>

namespace oasis {

constexpr const uint32_t READ_REQ_VADDR_ADDR = 0;
constexpr const uint32_t READ_REQ_SIZE_ADDR  = 1;
constexpr const uint32_t READ_REQ_CTID_ADDR  = 2;

ReadReqConfig::ReadReqConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                               uint32_t num_regs)
    : Config(cthread, addr_offset, num_regs), num_streams_(read_register(1).value()),
      base_vaddrs_(num_streams_, 0) {}

void ReadReqConfig::set_base_vaddr(libstf::stream_t stream, uintptr_t base_vaddr) {
    base_vaddrs_[stream] = base_vaddr;
}

void ReadReqConfig::set_ctid(libstf::stream_t stream, uint32_t ctid) {
    auto reg_offset = stream * READ_REQ_CONFIG_REGS;
    write_register(libstf::ConfigRegister(reg_offset + READ_REQ_CTID_ADDR, ctid));
}

void ReadReqConfig::enqueue_read(libstf::stream_t stream, size_t vaddr, size_t size) {
    auto reg_offset = stream * READ_REQ_CONFIG_REGS;
    write_register(
        libstf::ConfigRegister(reg_offset + READ_REQ_VADDR_ADDR, base_vaddrs_[stream] + vaddr));
    write_register(libstf::ConfigRegister(reg_offset + READ_REQ_SIZE_ADDR, size));
}

const libstf::stream_t ReadReqConfig::num_streams() const { return num_streams_; }

constexpr const uint32_t FILTER_LO_ADDR         = 0;
constexpr const uint32_t FILTER_HI_ADDR         = 1;
constexpr const uint32_t FILTER_CTRL_ADDR       = 2;
constexpr const uint32_t FILTER_LUT_MIN_ADDR    = 3;
constexpr const uint32_t FILTER_TABLE_ADDR_ADDR = 0;
constexpr const uint32_t FILTER_TABLE_DATA_ADDR = 1;
constexpr const uint32_t FILTER_TABLE_BITS_REG  = 4;
constexpr const uint32_t FILTER_FEATURES_REG    = 5;

FilterConfig::FilterConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                           uint32_t num_regs)
    : Config(cthread, addr_offset, num_regs), num_decoders_(read_register(1).value()),
      maximum_num_enqueued_confs_(read_register(2).value()), sel_rows_(read_register(3).value()) {
    // The address space tells the layout: a bitstream with the lookup table sizes it for four
    // registers per decoder and the two table registers.
    if (num_regs >= FILTER_CONFIG_REGS * num_decoders_ + FILTER_TABLE_REGS) {
        regs_per_decoder_ = FILTER_CONFIG_REGS;
        table_bits_       = read_register(FILTER_TABLE_BITS_REG).value();
        bloom_ = (read_register(FILTER_FEATURES_REG).value() & FILTER_FEATURE_BLOOM) != 0;
    } else {
        regs_per_decoder_ = FILTER_CONFIG_REGS_NO_LOOKUP;
        table_bits_       = 0;
        bloom_            = false;
    }
    table_words_.assign(table_bits_ / 64, 0);
    table_known_.assign(table_bits_ / 64, false);
}

void FilterConfig::enqueue(libstf::stream_t decoder, const FilterSpec &spec) {
    if (decoder >= num_decoders_) {
        throw std::runtime_error("attempted to configure the FilterStage of decoder " +
                                 std::to_string(decoder) + " (zero-based numbering), out of " +
                                 std::to_string(num_decoders_) + " decoders");
    }

    uint64_t ctrl = 0;
    ctrl |= spec.compare ? FILTER_CTRL_COMPARE : 0;
    ctrl |= spec.negate ? FILTER_CTRL_NEGATE : 0;
    ctrl |= spec.load_sel ? FILTER_CTRL_LOAD_SEL : 0;
    ctrl |= spec.store_sel ? FILTER_CTRL_STORE_SEL : 0;
    ctrl |= spec.emit ? FILTER_CTRL_EMIT : 0;
    if (spec.lookup) {
        uint32_t shift = spec.shift;
        if (spec.bloom) {
            // A Bloom filter of 2^k words: the hardware takes the word from the k low bits of the hash
            const size_t num_words = spec.lookup->words().size();
            if (!bloom_ || (num_words & (num_words - 1)) != 0) {
                throw std::runtime_error("FilterSpec looks up a Bloom filter that the FilterStage cannot take");
            }
            shift = libstf::floor_log2(num_words);
            ctrl |= FILTER_CTRL_BLOOM;
            ctrl |= spec.hash64 ? FILTER_CTRL_HASH64 : 0;
        } else if (table_bits_ == 0 || !spec.compare || spec.negate || spec.shift > FILTER_LOOKUP_MAX_SHIFT) {
            throw std::runtime_error("FilterSpec looks up a bitmap that the FilterStage cannot take");
        }
        ctrl |= FILTER_CTRL_LOOKUP;
        ctrl |= static_cast<uint64_t>(shift) << FILTER_CTRL_SHIFT_BIT;
        ctrl |= static_cast<uint64_t>(spec.lookup->base()) << FILTER_CTRL_BASE_BIT;
        if (!spec.lookup->written) {
            write_table(spec.lookup->base(), spec.lookup->words());
            spec.lookup->written = true;
        }
    }

    auto reg_offset = decoder * regs_per_decoder_;
    write_register(libstf::ConfigRegister(reg_offset + FILTER_LO_ADDR, static_cast<uint64_t>(spec.lo)));
    write_register(libstf::ConfigRegister(reg_offset + FILTER_HI_ADDR, static_cast<uint64_t>(spec.hi)));
    write_register(libstf::ConfigRegister(reg_offset + FILTER_CTRL_ADDR, ctrl));
    if (regs_per_decoder_ == FILTER_CONFIG_REGS) {
        write_register(libstf::ConfigRegister(reg_offset + FILTER_LUT_MIN_ADDR,
                                              static_cast<uint64_t>(spec.lut_min)));
    }
}

void FilterConfig::write_table(size_t base, const std::vector<uint64_t> &words) {
    if (base + words.size() > table_bits_ / 64) {
        throw std::runtime_error("lookup table region [" + std::to_string(base) + ", " +
                                 std::to_string(base + words.size()) + ") exceeds the " +
                                 std::to_string(table_bits_ / 64) + " words of the table");
    }

    // The address advances with every data write, so a run of changed words takes one address write
    auto   reg_offset = num_decoders_ * regs_per_decoder_;
    size_t next_addr  = table_words_.size();
    for (size_t i = 0; i < words.size(); i++) {
        auto addr = base + i;
        if (table_known_[addr] && table_words_[addr] == words[i]) {
            continue;
        }
        if (addr != next_addr) {
            write_register(libstf::ConfigRegister(reg_offset + FILTER_TABLE_ADDR_ADDR, addr));
        }
        write_register(libstf::ConfigRegister(reg_offset + FILTER_TABLE_DATA_ADDR, words[i]));
        table_words_[addr] = words[i];
        table_known_[addr] = true;
        next_addr          = addr + 1;
    }
}

const libstf::stream_t FilterConfig::num_decoders() const { return num_decoders_; }

const size_t FilterConfig::maximum_num_enqueued_confs() const {
    return maximum_num_enqueued_confs_;
}

const size_t FilterConfig::sel_rows() const { return sel_rows_; }

const size_t FilterConfig::table_bits() const { return table_bits_; }

const bool FilterConfig::supports_bloom() const { return bloom_; }

GenericConfig::GenericConfig(std::shared_ptr<coyote::cThread> cthread, uint32_t addr_offset,
                             uint32_t num_regs)
    : Config(cthread, addr_offset, num_regs) {}

uint64_t GenericConfig::notify_count() {
    return read_register(GENERIC_CONFIG_NOTIFY_COUNT_REG).value();
}

bool GenericConfig::has_bypass_profile() const {
    return num_regs >= GENERIC_CONFIG_BYPASS_PROFILE_REG + NUM_BYPASS_PROFILE_REGS;
}

parcore::StreamProfile GenericConfig::read_bypass_profile() {
    if (!has_bypass_profile()) {
        throw std::runtime_error(
            "Hardware design on device has no bypass StreamProfiler (non-RDMA build?)");
    }

    // Ascending order matters: the hardware resets the profiler when the last counter is read.
    auto base = GENERIC_CONFIG_BYPASS_PROFILE_REG;

    parcore::StreamProfile profile;
    profile.handshakes_cycles = read_register(base + 0).value();
    profile.starved_cycles    = read_register(base + 1).value();
    profile.stalled_cycles    = read_register(base + 2).value();
    profile.idle_cycles       = read_register(base + 3).value();
    profile.last_handshakes   = read_register(base + 4).value();
    return profile;
}

} // namespace oasis
