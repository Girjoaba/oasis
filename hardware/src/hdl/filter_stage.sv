`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * The filter of one decoder: compares the values of a column chunk against an interval, carries the
 * selection of the row group across its column chunks and removes the unselected values before the
 * output writer. One filter_conf_t per column chunk, in the order of the chunks, says what to do
 * (see filter_ctrl_t in the oasis package). The output of a chunk is its selected values as a
 * dense byte stream that ends with last, or a beat with keep 0 and last when no value is selected.
 *
 *   in -> FilterCompare -> FilterSelect -> FilterCompact -> out
 *
 * The stage holds a chunk back until its conf is there, like the decoder with its column chunk
 * conf, and takes the next conf only after the last beat of the chunk has entered. The
 * FilterCompare works on the beat at the input and the FilterSelect registers its pass bits and the
 * selection flags with it, so the bounds and the flags stay with the chunk they belong to. That
 * costs one idle cycle per chunk.
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

    typed_ndata_i.s in, // #(DATABEAT_SIZE)
    ndata_i.m out       // #(data8_t, DATABEAT_SIZE)
);

// -- Chunk configuration --------------------------------------------------------------------------
// conf_q holds the conf of the chunk in flight from its first beat till its last beat has entered.
filter_conf_t conf_q;
logic         conf_valid;

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

assign select_in.data  = in.data;
assign select_in.keep  = in.keep;
assign select_in.typ   = in.typ;
assign select_in.last  = in.last;
assign select_in.valid = in.valid && conf_valid;
assign in.ready        = select_in.ready && conf_valid;

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
    .valid(select_in.valid),
    .pass(pass)
);

// -- Select ---------------------------------------------------------------------------------------
logic[DATABEAT_SIZE / 4 - 1:0] select;

FilterSelect #(
    .DATABEAT_SIZE(DATABEAT_SIZE),
    .SEL_ROWS(SEL_ROWS)
) inst_select (
    .clk(clk),
    .rst_n(rst_n),

    .load_sel(conf_q.ctrl.load_sel),
    .store_sel(conf_q.ctrl.store_sel),
    .emit(conf_q.ctrl.emit),

    .pass(pass),

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
