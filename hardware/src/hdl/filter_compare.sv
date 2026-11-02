`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Compares every value of a typed ndata beat against the closed interval lo <= x <= hi of `conf`
 * and gives one pass bit per value in the same cycle. It only observes the beat: the FilterSelect
 * registers the pass bits with it. The selection bits of the conf (load_sel, store_sel, emit) are
 * for the FilterSelect.
 *
 * Lanes: 16 values per beat for 32 bit types, 8 values for 64 bit types (pass bits [7:0], the upper
 * bits are 0). A value passes if the keep bit of its lane is set and lo <= x <= hi (signed),
 * inverted with negate. Without compare every kept value passes. The compare is for INT32_T and
 * INT64_T; other types are rejected by a simulation assertion when compare is set.
 */
module FilterCompare
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE
) (
    input logic clk,
    input logic rst_n,

    input filter_conf_t conf,

    input data8_t[DATABEAT_SIZE - 1:0]    data,
    input logic[DATABEAT_SIZE - 1:0]      keep,
    input type_t                          typ,
    input logic                           valid, // Only for the type assertion
    output logic[DATABEAT_SIZE / 4 - 1:0] pass   // One bit per value of the beat
);

localparam NUM_32B_LANES = DATABEAT_SIZE / 4; // 32 bit elements per beat
localparam NUM_64B_LANES = DATABEAT_SIZE / 8; // 64 bit elements per beat

`ASSERT_ELAB(DATABEAT_SIZE % 8 == 0)

`ifndef SYNTHESIS
assert property (@(posedge clk) disable iff (!rst_n)
    !(valid && conf.ctrl.compare) || typ == INT32_T || typ == INT64_T)
else $fatal(1, "FilterCompare does not support type %0d", typ);
`endif

// -- Compare --------------------------------------------------------------------------------------
// Both widths share one set of 32 bit compares. The lane pair 2I, 2I + 1 holds two INT32 values or
// the low and high half of one INT64 value. For INT64 the even lane compares the low half unsigned
// and its result enters the compare of the odd lane as an extra low bit:
//   {x_high, x_low >= lo_low} >= {lo_high, 1}  <=>  x >= lo
//   {x_high, 1} <= {hi_high, x_low <= hi_low}  <=>  x <= hi
// For INT32 both extra bits are 1 and every lane compares its own value.
logic is_64;
assign is_64 = GET_TYPE_WIDTH(typ) == 64;

logic signed[32:0] lo_even, hi_even, lo_odd;
logic       [31:0] hi_odd;

assign lo_even = {!is_64 && conf.lo[31], conf.lo[31:0]}; // Unsigned for the low half of INT64
assign hi_even = {!is_64 && conf.hi[31], conf.hi[31:0]};
assign lo_odd  = {is_64 ? conf.lo[63:32] : conf.lo[31:0], 1'b1};
assign hi_odd  = is_64 ? conf.hi[63:32] : conf.hi[31:0];

logic[NUM_32B_LANES - 1:0] hit_32, keep_32;
logic[NUM_64B_LANES - 1:0] hit_64, keep_64;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic       [31:0] value_even, value_odd;
    logic signed[32:0] even, odd_ge, odd_le, hi_odd_le;
    logic              ge_even, le_even, ge_odd, le_odd;

    assign value_even = data[8 * I +: 4];
    assign value_odd  = data[8 * I + 4 +: 4];

    assign even    = {!is_64 && value_even[31], value_even};
    assign ge_even = even >= lo_even;
    assign le_even = even <= hi_even;

    assign odd_ge    = {value_odd, !is_64 || ge_even};
    assign odd_le    = {value_odd, 1'b1};
    assign hi_odd_le = {hi_odd, !is_64 || le_even};
    assign ge_odd    = odd_ge >= lo_odd;
    assign le_odd    = odd_le <= hi_odd_le;

    assign hit_32[2 * I]     = ge_even && le_even;
    assign hit_32[2 * I + 1] = ge_odd && le_odd;
    assign hit_64[I]         = ge_odd && le_odd;

    assign keep_32[2 * I]     = keep[8 * I];
    assign keep_32[2 * I + 1] = keep[8 * I + 4];
    assign keep_64[I]         = keep[8 * I];
end

logic[NUM_32B_LANES - 1:0] hit, lane_keep;

assign hit       = is_64 ? NUM_32B_LANES'(hit_64)  : hit_32;
assign lane_keep = is_64 ? NUM_32B_LANES'(keep_64) : keep_32;

assign pass = lane_keep & (conf.ctrl.compare ? hit ^ {NUM_32B_LANES{conf.ctrl.negate}} : '1);

endmodule
