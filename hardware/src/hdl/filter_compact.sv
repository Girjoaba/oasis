`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Removes the unselected 32 bit lanes of a typed ndata stream and packs the rest into full beats.
 * Every output beat is full except the last one of a chunk, as the StreamWriter requires. A chunk
 * without a selected lane ends in a beat with keep 0 and last, the empty stream of the StreamWriter.
 *
 * The lanes travel through the libstf DataNormalizer as filter_lane_t, 32 bits with the keep bits of
 * their bytes, so a partial last lane of a BYTE or INT16 column keeps its length. A 64 bit value
 * occupies two selected lanes, so it stays whole and 8 byte aligned. The type is dropped: the
 * output is the byte stream of the values.
 *
 * A skid buffer in front of the DataNormalizer registers the lanes, so that neither the selection,
 * which the FilterSelect reads from its bitmap RAM, nor the ready of the DataNormalizer stages has
 * a combinational path through the compaction.
 *
 * Full rate, one beat per cycle. At most one stall per chunk, when its last beat fills one output
 * beat and leaves a remainder.
 */
module FilterCompact
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE
) (
    input logic clk,
    input logic rst_n,

    input logic[DATABEAT_SIZE / 4 - 1:0] select, // One bit per 32 bit lane of in, valid with in.valid

    typed_ndata_i.s in, // #(DATABEAT_SIZE)
    ndata_i.m out       // #(data8_t, DATABEAT_SIZE)
);

localparam NUM_LANES = DATABEAT_SIZE / 4;

`ASSERT_ELAB(DATABEAT_SIZE % 4 == 0)

// -- Bytes to lanes -------------------------------------------------------------------------------
ndata_i #(filter_lane_t, NUM_LANES) lanes_in(clk, rst_n);
ndata_i #(filter_lane_t, NUM_LANES) lanes_out(clk, rst_n);

always_comb begin
    for (int i = 0; i < NUM_LANES; i++) begin
        lanes_in.data[i].data = in.data[4 * i +: 4];
        lanes_in.data[i].keep = in.keep[4 * i +: 4];
        lanes_in.keep[i]      = select[i] && |in.keep[4 * i +: 4];
    end
end

assign lanes_in.last  = in.last;
assign lanes_in.valid = in.valid;
assign in.ready       = lanes_in.ready;

// -- Compaction -----------------------------------------------------------------------------------
ndata_i #(filter_lane_t, NUM_LANES) lanes_reg(clk, rst_n);

NDataSkidBuffer #(filter_lane_t, NUM_LANES) inst_skid_buffer (
    .clk(clk),
    .rst_n(rst_n),

    .in(lanes_in),
    .out(lanes_reg)
);

DataNormalizer #(
    .data_t(filter_lane_t),
    .NUM_ELEMENTS(NUM_LANES),
    .ENABLE_COMPACTOR(1),
    .COMPACTOR_REGISTER_LEVELS(2),
    .BARREL_SHIFTER_REGISTER_LEVELS(1)
) inst_normalizer (
    .clk(clk),
    .rst_n(rst_n),

    .in(lanes_reg),
    .out(lanes_out)
);

// -- Lanes to bytes -------------------------------------------------------------------------------
always_comb begin
    for (int i = 0; i < NUM_LANES; i++) begin
        out.data[4 * i +: 4] = lanes_out.data[i].data;
        out.keep[4 * i +: 4] = lanes_out.keep[i] ? lanes_out.data[i].keep : '0;
    end
end

assign out.last        = lanes_out.last;
assign out.valid       = lanes_out.valid;
assign lanes_out.ready = out.ready;

endmodule
