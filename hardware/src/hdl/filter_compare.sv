`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Compares every value of a typed ndata stream against the closed interval of `bounds` and gives
 * one pass bit per value. The stream itself passes through unchanged with one register stage.
 *
 * Lanes: 16 values per beat for INT32_T, 8 values for INT64_T (pass bits [7:0], the upper bits are
 * 0). A value passes if the keep bit of its lane is set and lo <= x <= hi (signed), inverted with
 * negate. Without enable every kept value passes. Other types are rejected by a simulation
 * assertion when enable is set.
 *
 * The bounds are not latched per stream, so they have to stay stable while a stream passes.
 */
module FilterCompare
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE
) (
    input logic clk,
    input logic rst_n,

    input filter_bounds_t bounds,

    typed_ndata_i.s in,                         // #(DATABEAT_SIZE)
    typed_ndata_i.m out,                        // #(DATABEAT_SIZE)
    output logic[DATABEAT_SIZE / 4 - 1:0] pass  // One bit per value of out, valid with out.valid
);

localparam NUM_32B_LANES = DATABEAT_SIZE / 4; // 32 bit elements per beat
localparam NUM_64B_LANES = DATABEAT_SIZE / 8; // 64 bit elements per beat

`ASSERT_ELAB(DATABEAT_SIZE % 8 == 0)

`ifndef SYNTHESIS
assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && bounds.enable) || in.typ == INT32_T || in.typ == INT64_T)
else $fatal(1, "FilterCompare does not support type %0d", in.typ);
`endif

// -- Compare --------------------------------------------------------------------------------------
// Both widths share one set of 32 bit compares. The lane pair 2I, 2I + 1 holds two INT32 values or
// the low and high half of one INT64 value. For INT64 the even lane compares the low half unsigned
// and its result enters the compare of the odd lane as an extra low bit:
//   {x_high, x_low >= lo_low} >= {lo_high, 1}  <=>  x >= lo
//   {x_high, 1} <= {hi_high, x_low <= hi_low}  <=>  x <= hi
// For INT32 both extra bits are 1 and every lane compares its own value.
logic is_64;
assign is_64 = in.typ == INT64_T;

logic signed[32:0] lo_even, hi_even, lo_odd;
logic       [31:0] hi_odd;

assign lo_even = {!is_64 && bounds.lo[31], bounds.lo[31:0]}; // Unsigned for the low half of INT64
assign hi_even = {!is_64 && bounds.hi[31], bounds.hi[31:0]};
assign lo_odd  = {is_64 ? bounds.lo[63:32] : bounds.lo[31:0], 1'b1};
assign hi_odd  = is_64 ? bounds.hi[63:32] : bounds.hi[31:0];

logic[NUM_32B_LANES - 1:0] hit_32, keep_32;
logic[NUM_64B_LANES - 1:0] hit_64, keep_64;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic       [31:0] value_even, value_odd;
    logic signed[32:0] even, odd_ge, odd_le, hi_odd_le;
    logic              ge_even, le_even, ge_odd, le_odd;

    assign value_even = in.data[8 * I +: 4];
    assign value_odd  = in.data[8 * I + 4 +: 4];

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

    assign keep_32[2 * I]     = in.keep[8 * I];
    assign keep_32[2 * I + 1] = in.keep[8 * I + 4];
    assign keep_64[I]         = in.keep[8 * I];
end

logic[NUM_32B_LANES - 1:0] hit, lane_keep, n_pass;

assign hit       = in.typ == INT64_T ? NUM_32B_LANES'(hit_64)  : hit_32;
assign lane_keep = in.typ == INT64_T ? NUM_32B_LANES'(keep_64) : keep_32;

assign n_pass = lane_keep & (bounds.enable ? hit ^ {NUM_32B_LANES{bounds.negate}} : '1);

// -- Register stage -------------------------------------------------------------------------------
data8_t[DATABEAT_SIZE - 1:0] data;
logic  [DATABEAT_SIZE - 1:0] keep;
type_t                       typ;
logic                        last;
logic  [NUM_32B_LANES - 1:0] pass_q;
logic                        valid;

assign in.ready = !valid || out.ready;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        data   <= 'X;
        keep   <= 'X;
        typ    <= type_t'('X);
        last   <= 'X;
        pass_q <= 'X;
        valid  <= 1'b0;
    end else begin
        if (in.ready) begin
            data   <= in.data;
            keep   <= in.keep;
            typ    <= in.typ;
            last   <= in.last;
            pass_q <= n_pass;
            valid  <= in.valid;
        end
    end
end

assign out.data  = data;
assign out.keep  = keep;
assign out.typ   = typ;
assign out.last  = last;
assign out.valid = valid;
assign pass      = pass_q;

endmodule
