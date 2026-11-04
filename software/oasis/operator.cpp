#include "oasis/operator.hpp"

#include "oasis/configuration.hpp"
#include "oasis/oasis_context.hpp"
#include "parcore/configuration.hpp"

#include <cassert>
#include <cstdint>
#include <stdexcept>

namespace oasis {

void RDMASourceOperator::apply(libstf::stream_t stream, OasisContext &ctx) {
    ctx.config<ReadReqConfig>()->enqueue_read(stream, offset_, size_);
}

void RDMASourceOperator::print(std::ostream &os) const {
    os << "RDMASource(offset=" << offset_ << ", size=" << size_ << ")";
}

void LocalSourceOperator::apply(libstf::stream_t stream, OasisContext &ctx) {
    if (ctx.isRDMAEnabled()) {
        throw std::runtime_error("LocalSourceOperator cannot be used when RDMA is enabled");
    }

    // Coyote needs a 64B-aligned source address or it emits databeats with a broken keep signal.
    const auto &buffer = input_buffer_;
    assert((reinterpret_cast<uintptr_t>(buffer->ptr) % 64) == 0);
    ctx.tlb_manager()->ensure_tlb_mapping(buffer->ptr, buffer->capacity);

    ctx.config<ReadReqConfig>()->enqueue_read(stream, reinterpret_cast<uintptr_t>(buffer->ptr),
                                              buffer->size);
}

void LocalSourceOperator::print(std::ostream &os) const {
    os << "LocalSource(ptr=" << input_buffer_->ptr << ", size=" << input_buffer_->size << ")";
}

std::ostream &operator<<(std::ostream &os, const FilterSpec &spec) {
    os << "Filter(";
    if (spec.compare) {
        os << (spec.negate ? "not " : "") << "[" << spec.lo << ", " << spec.hi << "]";
    } else {
        os << "all";
    }
    if (spec.lookup) {
        os << ", bitmap " << spec.lut_min << " >> " << spec.shift << " @" << spec.lookup->base();
    }
    if (spec.load_sel) {
        os << ", load_sel";
    }
    if (spec.store_sel) {
        os << ", store_sel";
    }
    os << (spec.emit ? ", emit" : ", no emit") << ")";
    return os;
}

void DecodeColumnChunkOperator::apply(libstf::stream_t stream, OasisContext &ctx) {
    // The filter conf goes first: the hardware holds the chunk back until it has one. A bitmap
    // region reaches the tables with the first conf that looks it up. Only the dispatcher thread
    // applies the flows, so it writes every region once and before any chunk reads it.
    if (ctx.has_filter_config()) {
        ctx.config<FilterConfig>()->enqueue(stream, filter_);
    } else if (!filter_.is_passthrough()) {
        throw std::runtime_error(
            "Hardware design on device has no FilterConfig, but the flow filters a column chunk");
    }

    auto config = ctx.config<parcore::ColumnChunkDecoderConfig>();
    config->enqueue_column_chunk(stream, compression_, num_values_, type_, is_required_,
                                 logical_type_);
}

void DecodeColumnChunkOperator::print(std::ostream &os) const {
    os << "DecodeColumnChunk(compression=" << compression_ << ", num_values=" << num_values_
       << ", type=" << type_ << ", is_required=" << is_required_
       << ", logical_type=" << logical_type_ << ", filter=" << filter_ << ")";
}

void LocalSinkOperator::apply(libstf::stream_t stream, OasisContext &ctx) {
    for (const auto &buffer : buffers_) {
        ctx.enqueue_output_buffer(stream, *buffer);
    }
}

void LocalSinkOperator::print(std::ostream &os) const {
    size_t bytes = 0;
    for (const auto &buffer : buffers_) {
        bytes += buffer->size;
    }
    os << "HostBufferSink(tag=" << tag_ << ", buffers=" << buffers_.size() << ", size=" << bytes
       << ")";
}

} // namespace oasis
