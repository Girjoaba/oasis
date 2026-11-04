`timescale 1ns / 1ps

import lynxTypes::*;
import libstf::*;
import oasis::*;

`include "axi_macros.svh"
`include "config_macros.svh"

parameter DATABEAT_SIZE = 64;

// -- Tie-off unused interfaces and signals --------------------------------------------------------
always_comb notify.tie_off_m();
always_comb sq_rd.tie_off_m();
always_comb sq_wr.tie_off_m();
always_comb cq_rd.tie_off_s();
always_comb cq_wr.tie_off_s();

for (genvar I = 1; I < N_STRM_AXI; I++) begin
    always_comb axis_host_recv[I].tie_off_s();
end

// -- Fix clock and reset names --------------------------------------------------------------------
logic clk;
logic rst_n;

assign clk   = aclk;
assign rst_n = aresetn;

// -- Signals --------------------------------------------------------------------------------------
AXI4S axi_host_recv_0(.aclk(clk), .aresetn(rst_n));

typed_ndata_i #(DATABEAT_SIZE) in(clk, rst_n);
logic[DATABEAT_SIZE / 4 - 1:0] pass;

typed_ndata_i #(DATABEAT_SIZE) values(clk, rst_n);
typed_ndata_i #(DATABEAT_SIZE) mask(clk, rst_n);

AXI4S axi_out[N_STRM_AXI](.aclk(clk), .aresetn(rst_n));

// -- Configuration -------------------------------------------------------------------------------
write_config_i write_configs[2](clk, rst_n);
read_config_i  read_configs [2](clk, rst_n);
GlobalConfig #(
    .SYSTEM_ID(OASIS_SYSTEM_ID),
    .NUM_CONFIGS(2),
    .ADDR_SPACE_SIZES({2, FILTER_CONFIG_REGS(1)})
) inst_config (
    .clk(clk),
    .rst_n(rst_n),

    .axi_ctrl(axi_ctrl),

    .write_configs(write_configs),
    .read_configs(read_configs)
);

stream_config_i stream_config[1](clk, rst_n);
StreamConfig #(
    .NUM_STREAMS(1)
) inst_stream_config (
    .clk(clk),
    .rst_n(rst_n),

    .write_config(write_configs[0]),
    .read_config(read_configs[0]),

    .out(stream_config)
);

ready_valid_i #(type_t) data_type(clk, rst_n);
`CONFIG_SIGNALS_TO_INTF(stream_config[0].data_type, data_type)

assign stream_config[0].select_ready = 1'b1;

// The bounds of the test come from the first conf of the FilterConfig and hold for the whole test
ready_valid_i #(filter_conf_t) filter_conf[1](clk, rst_n);
FilterConfig #(
    .NUM_DECODERS(1)
) inst_filter_config (
    .clk(clk),
    .rst_n(rst_n),

    .write_config(write_configs[1]),
    .read_config(read_configs[1]),

    .out(filter_conf),

    .table_write_valid(),
    .table_write()
);

filter_conf_t conf;
assign filter_conf[0].ready = 1'b1;

always_ff @(posedge clk) begin
    if (!rst_n) begin
        conf <= '0;
    end else if (filter_conf[0].valid) begin
        conf <= filter_conf[0].data;
    end
end

// -- Input ----------------------------------------------------------------------------------------
`AXIS_ASSIGN(axis_host_recv[0], axi_host_recv_0) // AXI4SR to AXI4S
AXIToTypedNData #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_axi_to_data (
    .clk(clk),
    .rst_n(rst_n),

    .in_type(data_type),

    .in(axi_host_recv_0),
    .out(in)
);

// -- Compare --------------------------------------------------------------------------------------
FilterCompare #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_filter_compare (
    .clk(clk),
    .rst_n(rst_n),

    .conf(conf),

    .data(in.data),
    .keep(in.keep),
    .typ(in.typ),
    .valid(in.valid),
    .pass(pass)
);

// -- Output duplication ---------------------------------------------------------------------------
// Stream 0 gets the values and stream 1 the pass bits in the shape of the values: one element per
// value, 1 if the value passes and 0 otherwise.
typedef struct packed {
    data8_t[DATABEAT_SIZE - 1:0]   data;
    type_t                         typ;
    logic[DATABEAT_SIZE - 1:0]     keep;
    logic                          last;
    logic[DATABEAT_SIZE / 4 - 1:0] pass;
} beat_t;

ready_valid_i #(beat_t) beat(clk, rst_n);
ready_valid_i #(beat_t) beat_dup[2](clk, rst_n);

assign beat.data.data = in.data;
assign beat.data.typ  = in.typ;
assign beat.data.keep = in.keep;
assign beat.data.last = in.last;
assign beat.data.pass = pass;
assign beat.valid     = in.valid;
assign in.ready       = beat.ready;

ReadyValidDuplicator #(2) inst_beat_duplicate (
    .clk(clk),
    .rst_n(rst_n),

    .in(beat),
    .out(beat_dup)
);

assign values.data       = beat_dup[0].data.data;
assign values.typ        = beat_dup[0].data.typ;
assign values.keep       = beat_dup[0].data.keep;
assign values.last       = beat_dup[0].data.last;
assign values.valid      = beat_dup[0].valid;
assign beat_dup[0].ready = values.ready;

always_comb begin
    mask.data = '0;
    for (int i = 0; i < DATABEAT_SIZE / 4; i++) begin
        if (beat_dup[1].data.typ == INT64_T) begin
            if (i < DATABEAT_SIZE / 8) begin
                mask.data[8 * i] = data8_t'(beat_dup[1].data.pass[i]);
            end
        end else begin
            mask.data[4 * i] = data8_t'(beat_dup[1].data.pass[i]);
        end
    end
end

assign mask.typ          = beat_dup[1].data.typ;
assign mask.keep         = beat_dup[1].data.keep;
assign mask.last         = beat_dup[1].data.last;
assign mask.valid        = beat_dup[1].valid;
assign beat_dup[1].ready = mask.ready;

// -- Output ---------------------------------------------------------------------------------------
TypedNDataToAXI #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_values_to_axi (
    .clk(clk),
    .rst_n(rst_n),

    .in(values),
    .out(axi_out[0])
);

TypedNDataToAXI #(
    .DATABEAT_SIZE(DATABEAT_SIZE)
) inst_mask_to_axi (
    .clk(clk),
    .rst_n(rst_n),

    .in(mask),
    .out(axi_out[1])
);

for (genvar I = 2; I < N_STRM_AXI; I++) begin
    always_comb axi_out[I].tie_off_m();
end

for (genvar I = 0; I < N_STRM_AXI; I++) begin
    `AXIS_ASSIGN(axi_out[I], axis_host_send[I])
end
