`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Looks every value of a typed ndata beat up in the lookup table (FilterTable) and ANDs its bit into
 * the pass bits of the FilterCompare. The beat, its pass bits and the selection bits of its conf
 * travel through the pipeline together, so the conf of the chunk may change once its last beat has
 * entered.
 *
 * With lookup, a value x of the conf passes if it passed the compare and the bit
 * base * 64 + ((x - lut_min) >> shift) of the table is set. The difference is taken in the width
 * of the value, INT32_T or INT64_T, so the software only has to keep the compare interval inside
 * the region of the bitmap. The index of a value that fails the compare may leave the region; its
 * pass bit stays 0. Without lookup the pass bits pass through unchanged.
 *
 * Lanes as in the FilterCompare: 16 values per beat for 32 bit types, 8 for 64 bit types (pass bits
 * [7:0]). The lane pair 2I, 2I + 1 holds two INT32 values or one INT64 value, which the odd lane of
 * the pair looks up.
 *
 * Pipeline: input register, difference, shift, index, FILTER_TABLE_LATENCY cycles of the table,
 * then a skid buffer. All stages move with one enable, which is low while a table word is written
 * (the write takes port A of the table) and while the output holds a beat that the skid buffer
 * cannot take. The skid buffer keeps the ready of the next stage off that enable. Full rate, one
 * beat per cycle except the cycles of the table writes.
 */
module FilterLookup
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE
) (
    input logic clk,
    input logic rst_n,

    input filter_conf_t                  conf, // Valid with in.valid
    input logic[DATABEAT_SIZE / 4 - 1:0] pass, // Valid with in.valid

    input logic                table_write_valid,
    input filter_table_write_t table_write,

    typed_ndata_i.s in,  // #(DATABEAT_SIZE)
    typed_ndata_i.m out, // #(DATABEAT_SIZE)

    output logic                          out_load_sel,  // Valid with out.valid
    output logic                          out_store_sel, // Valid with out.valid
    output logic                          out_emit,      // Valid with out.valid
    output logic[DATABEAT_SIZE / 4 - 1:0] out_pass       // Valid with out.valid
);

localparam NUM_32B_LANES = DATABEAT_SIZE / 4;
localparam NUM_64B_LANES = DATABEAT_SIZE / 8;
localparam INDEX_BITS    = FILTER_TABLE_WORD_BITS + 6;
localparam WIDE_BITS     = 32 + INDEX_BITS - 1; // The bits of a 64 bit difference a shift <= 31 can reach
localparam NUM_STAGES    = 4 + FILTER_TABLE_LATENCY;

`ASSERT_ELAB(DATABEAT_SIZE % 8 == 0)
`ASSERT_ELAB(INDEX_BITS <= 32)

typedef struct packed {
    data8_t[DATABEAT_SIZE - 1:0] data;
    logic[DATABEAT_SIZE - 1:0]   keep;
    type_t                       typ;
    logic                        last;
    logic[NUM_32B_LANES - 1:0]   pass;
    logic                        load_sel;
    logic                        store_sel;
    logic                        emit;
    logic                        lookup;
} beat_t;

`ifndef SYNTHESIS
assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && conf.ctrl.lookup) || (in.typ == INT32_T || in.typ == INT64_T))
else $fatal(1, "FilterLookup does not support type %0d", in.typ);

assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && conf.ctrl.lookup) || (conf.ctrl.compare && !conf.ctrl.negate && conf.ctrl.shift < 32))
else $fatal(1, "FilterLookup needs compare without negate and a shift below 32");
`endif

// -- Pipeline -------------------------------------------------------------------------------------
ready_valid_i #(beat_t) skid_in(clk, rst_n), skid_out(clk, rst_n);

beat_t beat_q [NUM_STAGES];
logic  valid_q[NUM_STAGES];

(* max_fanout = 64 *) logic enable;
assign enable   = (!valid_q[NUM_STAGES - 1] || skid_in.ready) && !table_write_valid;
assign in.ready = enable;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        for (int S = 0; S < NUM_STAGES; S++) begin
            valid_q[S] <= 1'b0;
        end
    end else begin
        if (enable) begin
            valid_q[0] <= in.valid;
            for (int S = 1; S < NUM_STAGES; S++) begin
                valid_q[S] <= valid_q[S - 1];
            end
        end
    end
end

always_ff @(posedge clk) begin
    if (enable) begin
        beat_q[0].data      <= in.data;
        beat_q[0].keep      <= in.keep;
        beat_q[0].typ       <= in.typ;
        beat_q[0].last      <= in.last;
        beat_q[0].pass      <= pass;
        beat_q[0].load_sel  <= conf.ctrl.load_sel;
        beat_q[0].store_sel <= conf.ctrl.store_sel;
        beat_q[0].emit      <= conf.ctrl.emit;
        beat_q[0].lookup    <= conf.ctrl.lookup;
        for (int S = 1; S < NUM_STAGES; S++) begin
            beat_q[S] <= beat_q[S - 1];
        end
    end
end

// -- Stage 0 to 1: difference ---------------------------------------------------------------------
// One 32 bit subtract per lane. For INT64 the borrow of the even lane enters the odd lane, as the
// lane pairs of the FilterCompare do, so the pair holds the 64 bit difference.
data64_t   lut_min_q;
logic[4:0] shift_q[2];
logic[FILTER_TABLE_WORD_BITS - 1:0] base_q[3];

always_ff @(posedge clk) begin
    if (enable) begin
        lut_min_q  <= conf.lut_min;
        shift_q[0] <= conf.ctrl.shift[4:0];
        shift_q[1] <= shift_q[0];
        base_q[0]  <= conf.ctrl.base[FILTER_TABLE_WORD_BITS - 1:0];
        base_q[1]  <= base_q[0];
        base_q[2]  <= base_q[1];
    end
end

logic is_64_0, is_64_1;
assign is_64_0 = GET_TYPE_WIDTH(beat_q[0].typ) == 64;
assign is_64_1 = GET_TYPE_WIDTH(beat_q[1].typ) == 64;

logic[NUM_32B_LANES - 1:0][31:0] diff, diff_q;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic[31:0] value_even, value_odd, min_even, min_odd;
    logic       borrow_even;

    assign value_even = beat_q[0].data[8 * I +: 4];
    assign value_odd  = beat_q[0].data[8 * I + 4 +: 4];
    assign min_even   = lut_min_q[31:0];
    assign min_odd    = is_64_0 ? lut_min_q[63:32] : lut_min_q[31:0];

    assign {borrow_even, diff[2 * I]} = {1'b0, value_even} + {1'b0, ~min_even} + 33'd1;
    assign diff[2 * I + 1]            = value_odd + ~min_odd + 32'(!is_64_0 || borrow_even);
end

always_ff @(posedge clk) begin
    if (enable) begin
        diff_q <= diff;
    end
end

// -- Stage 1 to 2: shift --------------------------------------------------------------------------
logic[NUM_32B_LANES - 1:0][INDEX_BITS - 1:0] offset_q;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic[WIDE_BITS - 1:0] wide_odd;

    assign wide_odd = is_64_1 ? {diff_q[2 * I + 1][WIDE_BITS - 33:0], diff_q[2 * I]} : WIDE_BITS'(diff_q[2 * I + 1]);

    always_ff @(posedge clk) begin
        if (enable) begin
            offset_q[2 * I]     <= INDEX_BITS'(diff_q[2 * I] >> shift_q[1]);
            offset_q[2 * I + 1] <= INDEX_BITS'(wide_odd >> shift_q[1]);
        end
    end
end

// -- Stage 2 to 3: bit index and table read -------------------------------------------------------
logic[NUM_32B_LANES - 1:0][INDEX_BITS - 1:0] index_q;
logic[NUM_32B_LANES - 1:0]                   bits;

always_ff @(posedge clk) begin
    if (enable) begin
        for (int I = 0; I < NUM_32B_LANES; I++) begin
            index_q[I] <= offset_q[I] + {base_q[2], 6'b0};
        end
    end
end

FilterTable #(
    .NUM_LANES(NUM_32B_LANES)
) inst_table (
    .clk(clk),

    .table_write_valid(table_write_valid),
    .table_write(table_write),

    .read_enable(enable),
    .read_bit(index_q),
    .bits(bits)
);

// -- Output ---------------------------------------------------------------------------------------
// The bits of the table belong to the beat in the last stage. A 64 bit value has its bit in the odd
// lane of its pair.
logic[NUM_32B_LANES - 1:0] hit;
logic                      is_64_out;
beat_t                     last_beat;

assign last_beat = beat_q[NUM_STAGES - 1];
assign is_64_out = GET_TYPE_WIDTH(last_beat.typ) == 64;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    assign hit[I] = is_64_out ? bits[2 * I + 1] : bits[I];
end
for (genvar I = NUM_64B_LANES; I < NUM_32B_LANES; I++) begin
    assign hit[I] = is_64_out ? 1'b0 : bits[I];
end

always_comb begin
    skid_in.data      = last_beat;
    skid_in.data.pass = last_beat.pass & (last_beat.lookup ? hit : '1);
end

assign skid_in.valid = valid_q[NUM_STAGES - 1] && !table_write_valid;

SkidBuffer #(
    .data_t(beat_t)
) inst_skid_buffer (
    .clk(clk),
    .rst_n(rst_n),

    .in(skid_in),
    .out(skid_out)
);

assign out.data       = skid_out.data.data;
assign out.keep       = skid_out.data.keep;
assign out.typ        = skid_out.data.typ;
assign out.last       = skid_out.data.last;
assign out.valid      = skid_out.valid;
assign skid_out.ready = out.ready;

assign out_load_sel  = skid_out.data.load_sel;
assign out_store_sel = skid_out.data.store_sel;
assign out_emit      = skid_out.data.emit;
assign out_pass      = skid_out.data.pass;

endmodule
