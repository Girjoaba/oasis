`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * The filter of one decoder: compares the values of a column chunk against an interval, looks them
 * up in a bitmap, carries the selection of the row group across its column chunks and removes the
 * unselected values before the output writer. One filter_conf_t per column chunk, in the order of the chunks, says what to do
 * (see filter_ctrl_t in the oasis package). The output of a chunk is its selected values as a
 * dense byte stream that ends with last, or a beat with keep 0 and last when no value is selected.
 *
 *   in -> FilterCompare -> FilterLookup -> FilterSelect -> FilterCompact -> out
 *
 * The stage holds a chunk back until its conf is there, like the decoder with its column chunk
 * conf, and takes the next conf only after the last beat of the chunk has entered. The
 * FilterCompare works on the beat at the input and the FilterLookup registers its pass bits and the
 * selection flags with it, so the bounds and the flags stay with the chunk they belong to. That
 * costs one idle cycle per chunk.
 *
 * The lookup table (FilterTable in the FilterLookup) is written through table_write, by the
 * FilterConfig of all decoders. With FILTER_TABLE_BITS 0 the stage has no FilterLookup and the
 * FilterSelect registers the pass bits.
 */
module FilterStage
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE,
    parameter SEL_ROWS = FILTER_SEL_ROWS
) (
    input logic clk,
    input logic rst_n,

    ready_valid_i.s conf, // #(filter_conf_t)

    input logic                table_write_valid,
    input filter_table_write_t table_write,

    typed_ndata_i.s in, // #(DATABEAT_SIZE)
    ndata_i.m out       // #(data8_t, DATABEAT_SIZE)
);

// -- Chunk configuration --------------------------------------------------------------------------
// conf_q holds the conf of the chunk in flight from its first beat till its last beat has entered.
filter_conf_t conf_q;
logic         conf_valid;

typed_ndata_i #(DATABEAT_SIZE) stage_in(clk, rst_n);
typed_ndata_i #(DATABEAT_SIZE) select_in(clk, rst_n);
typed_ndata_i #(DATABEAT_SIZE) select_out(clk, rst_n);

assign conf.ready = !conf_valid;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        conf_q     <= 'X;
        conf_valid <= 1'b0;
    end else begin
        if (!conf_valid) begin
            if (conf.valid) begin
                conf_q     <= conf.data;
                conf_valid <= 1'b1;
            end
        end else begin
            if (in.valid && in.ready && in.last) begin
                conf_valid <= 1'b0;
            end
        end
    end
end

assign stage_in.data  = in.data;
assign stage_in.keep  = in.keep;
assign stage_in.typ   = in.typ;
assign stage_in.last  = in.last;
assign stage_in.valid = in.valid && conf_valid;
assign in.ready       = stage_in.ready && conf_valid;

// -- Compare --------------------------------------------------------------------------------------
logic[DATABEAT_SIZE / 4 - 1:0] pass;

FilterCompare #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_compare (
    .clk(clk),
    .rst_n(rst_n),

    .conf(conf_q),

    .data(in.data),
    .keep(in.keep),
    .typ(in.typ),
    .valid(stage_in.valid),
    .pass(pass)
);

// -- Lookup ---------------------------------------------------------------------------------------
logic                          load_sel, store_sel, emit;
logic[DATABEAT_SIZE / 4 - 1:0] select_pass;

if (FILTER_TABLE_BITS > 0) begin : gen_lookup
    FilterLookup #(
        .DATABEAT_SIZE(DATABEAT_SIZE)
    ) inst_lookup (
        .clk(clk),
        .rst_n(rst_n),

        .conf(conf_q),
        .pass(pass),

        .table_write_valid(table_write_valid),
        .table_write(table_write),

        .in(stage_in),
        .out(select_in),

        .out_load_sel(load_sel),
        .out_store_sel(store_sel),
        .out_emit(emit),
        .out_pass(select_pass)
    );
end else begin : gen_no_lookup
    `DATA_ASSIGN(stage_in, select_in)
    assign select_in.typ = stage_in.typ;

    assign load_sel    = conf_q.ctrl.load_sel;
    assign store_sel   = conf_q.ctrl.store_sel;
    assign emit        = conf_q.ctrl.emit;
    assign select_pass = pass;
end

// -- Select ---------------------------------------------------------------------------------------
logic[DATABEAT_SIZE / 4 - 1:0] select;

FilterSelect #(
    .DATABEAT_SIZE(DATABEAT_SIZE),
    .SEL_ROWS(SEL_ROWS)
) inst_select (
    .clk(clk),
    .rst_n(rst_n),

    .load_sel(load_sel),
    .store_sel(store_sel),
    .emit(emit),

    .pass(select_pass),

    .in(select_in),
    .out(select_out),
    .select(select)
);

// -- Compact --------------------------------------------------------------------------------------
FilterCompact #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_compact (
    .clk(clk),
    .rst_n(rst_n),

    .select(select),

    .in(select_out),
    .out(out)
);

endmodule
