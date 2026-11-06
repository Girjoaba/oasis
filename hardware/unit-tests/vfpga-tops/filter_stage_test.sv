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

// -- Fix clock and reset names --------------------------------------------------------------------
logic clk;
logic rst_n;

assign clk   = aclk;
assign rst_n = aresetn;

// -- Configuration -------------------------------------------------------------------------------
// The StreamConfig gives the type of every chunk, the FilterConfig its filter conf and the words of
// the lookup tables.
localparam STREAM_CONFIG_REGS = N_STRM_AXI > 2 ? N_STRM_AXI : 2;

write_config_i write_configs[2](clk, rst_n);
read_config_i  read_configs [2](clk, rst_n);
GlobalConfig #(
    .SYSTEM_ID(OASIS_SYSTEM_ID),
    .NUM_CONFIGS(2),
    .ADDR_SPACE_SIZES({STREAM_CONFIG_REGS, FILTER_CONFIG_REGS(N_STRM_AXI)})
) inst_config (
    .clk(clk),
    .rst_n(rst_n),

    .axi_ctrl(axi_ctrl),

    .write_configs(write_configs),
    .read_configs(read_configs)
);

stream_config_i stream_config[N_STRM_AXI](clk, rst_n);
StreamConfig #(
    .NUM_STREAMS(N_STRM_AXI)
) inst_stream_config (
    .clk(clk),
    .rst_n(rst_n),

    .write_config(write_configs[0]),
    .read_config(read_configs[0]),

    .out(stream_config)
);

ready_valid_i #(filter_conf_t) filter_conf[N_STRM_AXI](clk, rst_n);
logic                          filter_table_write_valid;
filter_table_write_t           filter_table_write;
FilterConfig #(
    .NUM_DECODERS(N_STRM_AXI)
) inst_filter_config (
    .clk(clk),
    .rst_n(rst_n),

    .write_config(write_configs[1]),
    .read_config(read_configs[1]),

    .out(filter_conf),

    .table_write_valid(filter_table_write_valid),
    .table_write(filter_table_write)
);

// -- One FilterStage per stream -------------------------------------------------------------------
// Stream I takes the chunks of decoder I: AXI in, typed by the StreamConfig, through the FilterStage
// and back to AXI. The host does not expect any data for empty chunks, so their null beat is dropped.
// As in the StreamWriter, a null last beat after the data of a chunk first moves onto the beat before
// it, so that only the null beat of an empty chunk is left to drop.
// The output stalls 3 cycles in every 8, so that the beats queue up inside the stage.
for (genvar I = 0; I < N_STRM_AXI; I++) begin : gen_stages
    AXI4S axi_in(.aclk(clk), .aresetn(rst_n));
    AXI4S axi_out(.aclk(clk), .aresetn(rst_n));

    typed_ndata_i #(DATABEAT_SIZE)          in(clk, rst_n);
    ndata_i #(data8_t, DATABEAT_SIZE)       filtered(clk, rst_n);

    ready_valid_i #(type_t) data_type(clk, rst_n);
    `CONFIG_SIGNALS_TO_INTF(stream_config[I].data_type, data_type)
    assign stream_config[I].select_ready = 1'b1;

    `AXIS_ASSIGN(axis_host_recv[I], axi_in) // AXI4SR to AXI4S
    AXIToTypedNData #(
        .DATABEAT_SIZE(DATABEAT_SIZE)
    ) inst_axi_to_data (
        .clk(clk),
        .rst_n(rst_n),

        .in_type(data_type),

        .in(axi_in),
        .out(in)
    );

    FilterStage #(
        .DATABEAT_SIZE(DATABEAT_SIZE)
    ) inst_filter_stage (
        .clk(clk),
        .rst_n(rst_n),

        .conf(filter_conf[I]),

        .table_write_valid(filter_table_write_valid),
        .table_write(filter_table_write),

        .in(in),
        .out(filtered)
    );

    NDataToAXI #(data8_t, DATABEAT_SIZE) inst_ndata_to_axi (
        .clk(clk),
        .rst_n(rst_n),

        .in(filtered),
        .out(axi_out)
    );

    logic[2:0] stall_count;
    logic      stall;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            stall_count <= '0;
        end else begin
            stall_count <= stall_count + 1;
        end
    end

    assign stall = stall_count < 3;

    AXI4S axi_no_nulls(.aclk(clk), .aresetn(rst_n));
    AXINullBeatSuppressor inst_null_beat_suppressor (
        .clk(clk),
        .rst_n(rst_n),

        .in(axi_out),
        .out(axi_no_nulls)
    );

    assign axis_host_send[I].tdata  = axi_no_nulls.tdata;
    assign axis_host_send[I].tkeep  = axi_no_nulls.tkeep;
    assign axis_host_send[I].tlast  = axi_no_nulls.tlast;
    assign axis_host_send[I].tvalid = axi_no_nulls.tvalid && axi_no_nulls.tkeep != '0 && !stall;
    assign axi_no_nulls.tready      = !stall && (axis_host_send[I].tready ||
                                                 (axi_no_nulls.tvalid && axi_no_nulls.tkeep == '0));

`ifndef SYNTHESIS
    // Every input chunk has to leave the stage as exactly one output stream, also an empty one.
    int unsigned chunks_in, chunks_out;

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            chunks_in  <= 0;
            chunks_out <= 0;
        end else begin
            if (in.valid && in.ready && in.last) begin
                chunks_in <= chunks_in + 1;
            end
            if (axi_out.tvalid && axi_out.tready && axi_out.tlast) begin
                chunks_out <= chunks_out + 1;
                if (chunks_out >= chunks_in) begin
                    $fatal(1, "FilterStage %0d ended an output stream without an input chunk", I);
                end
            end
            if (axi_out.tvalid && axi_out.tready && !axi_out.tlast && axi_out.tkeep != '1) begin
                $fatal(1, "FilterStage %0d emitted a partial beat that is not last", I);
            end
        end
    end
`endif
end : gen_stages
