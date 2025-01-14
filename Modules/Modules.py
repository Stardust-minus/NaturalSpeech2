from argparse import Namespace
import torch
import numpy as np
import math
from typing import Optional, Union
from encodec import EncodecModel
from einops import rearrange
from random import sample

from .Nvidia_Alignment_Learning_Framework import Alignment_Learning_Framework
from .Diffusion import Diffusion
from .LinearAttention import LinearAttention
from .Layer import Conv1d, RMSNorm


class NaturalSpeech2(torch.nn.Module):
    def __init__(
        self,
        hyper_parameters: Namespace,
        latent_mean: float,
        latent_std: float,
        ):
        super().__init__()
        self.hp = hyper_parameters
        self.latent_mean = latent_mean
        self.latent_std = latent_std

        self.encoder = Phoneme_Encoder(self.hp)
        self.speech_prompter = Speech_Prompter(self.hp)

        self.alignment_learning_framework = Alignment_Learning_Framework(
            feature_size= self.hp.Sound.Mel_Dim,
            encoding_size= self.hp.Encoder.Size,
            condition_channels= self.hp.Speech_Prompter.Size,
            condition_attenion_head= self.hp.Alignment_Learning_Framework.Condition_Attention_Head
            )

        self.variance_block = Variacne_Block(self.hp)

        self.diffusion = Diffusion(self.hp)

        self.encodec = EncodecModel.encodec_model_24khz()

        self.segment = Segment()

        self.ce_rvq = CE_RVQ(
            encodec= self.encodec,
            rvq_sample= self.hp.Diffusion.CERVQ.Num_Sample,
            use_weighted_sample= self.hp.Diffusion.CERVQ.Use_Weighted_Sample
            )

    def forward(
        self,
        tokens: torch.LongTensor,
        token_lengths: torch.LongTensor,
        speech_prompts: torch.LongTensor,
        speech_prompts_for_diffusion: Optional[torch.LongTensor]= None,
        latents: Optional[torch.LongTensor]= None,
        latent_lengths: Optional[torch.LongTensor]= None,
        f0s: Optional[torch.FloatTensor]= None,
        mels: Optional[torch.FloatTensor]= None,
        attention_priors: Optional[torch.FloatTensor]= None,
        ddim_steps: Optional[int]= None
        ):
        if all([
            not speech_prompts_for_diffusion is None,
            not latents is None,
            not latent_lengths is None,
            not f0s is None,
            not mels is None,
            not attention_priors is None
            ]):    # train
            return self.Train(
                tokens= tokens,
                token_lengths= token_lengths,
                speech_prompts= speech_prompts,
                speech_prompts_for_diffusion= speech_prompts_for_diffusion,
                latents= latents,
                latent_lengths= latent_lengths,
                f0s= f0s,
                mels= mels,
                attention_priors= attention_priors
                )
        else:   #  inference
            return self.Inference(
                tokens= tokens,
                token_lengths= token_lengths,
                speech_prompts= speech_prompts,
                ddim_steps= ddim_steps
                )

    def Train(
        self,
        tokens: torch.LongTensor,
        token_lengths: torch.LongTensor,
        speech_prompts: torch.LongTensor,
        speech_prompts_for_diffusion: torch.LongTensor,
        latents: torch.LongTensor,
        latent_lengths: torch.LongTensor,
        f0s: torch.FloatTensor,
        mels: torch.Tensor,
        attention_priors: torch.Tensor,
        ):
        latent_codes = latents
        with torch.no_grad():
            latents = self.encodec.quantizer.decode(latents.permute(1, 0, 2))
            latents = (latents - self.latent_mean) / self.latent_std
            speech_prompts = self.encodec.quantizer.decode(speech_prompts.permute(1, 0, 2))
            speech_prompts_for_diffusion = self.encodec.quantizer.decode(speech_prompts_for_diffusion.permute(1, 0, 2))

        encodings = self.encoder(
            tokens= tokens,
            lengths= token_lengths
            )
        speech_prompts = self.speech_prompter(speech_prompts)
        speech_prompts_for_diffusion = self.speech_prompter(speech_prompts_for_diffusion)

        durations, attention_softs, attention_hards, attention_logprobs = self.alignment_learning_framework(
            token_embeddings= self.encoder.token_embedding(tokens).permute(0, 2, 1),
            encoding_lengths= token_lengths,
            conditions= speech_prompts,
            features= mels,
            feature_lengths= latent_lengths,
            attention_priors= attention_priors
            )

        encodings_expand, duration_predictions, f0_predictions, _, _, _ = self.variance_block(
            encodings= encodings,
            encoding_lengths= token_lengths,
            speech_prompts= speech_prompts,
            durations= durations,
            f0s= f0s,
            latent_lengths= latent_lengths
            )
        
        encodings_expand_slice, offsets = self.segment(
            patterns= encodings_expand.permute(0, 2, 1),
            segment_size= self.hp.Train.Segment_Size,
            lengths= latent_lengths
            )        
        encodings_expand_slice = encodings_expand_slice.permute(0, 2, 1)
        
        latent_codes_slice, _ = self.segment(
            patterns= latent_codes.permute(0, 2, 1),
            segment_size= self.hp.Train.Segment_Size,
            offsets= offsets
            )
        latent_codes_slice = latent_codes_slice.permute(0, 2, 1)

        latents_slice, _ = self.segment(
            patterns= latents.permute(0, 2, 1),
            segment_size= self.hp.Train.Segment_Size,
            offsets= offsets
            )
        latents_slice = latents_slice.permute(0, 2, 1)

        _, diffusion_targets, diffusion_predictions, diffusion_starts = self.diffusion(
            encodings= encodings_expand_slice,
            lengths= torch.full_like(latent_lengths, fill_value= self.hp.Train.Segment_Size),
            speech_prompts= speech_prompts_for_diffusion,
            latents= latents_slice
            )
        
        ce_rvq_losses = self.ce_rvq(
            diffusion_starts= diffusion_starts * self.latent_std + self.latent_mean,
            target_latent_codes= latent_codes_slice
            )

        return \
            None, latents_slice, diffusion_starts, diffusion_targets, diffusion_predictions, \
            duration_predictions, f0_predictions, ce_rvq_losses, \
            attention_softs, attention_hards, attention_logprobs, durations, None, None

    def Inference(
        self,
        tokens: torch.LongTensor,
        token_lengths: torch.LongTensor,
        speech_prompts: torch.LongTensor,
        ddim_steps: Optional[int]= None,
        temperature: float= 1.2 ** 2    # ignore now
        ):
        speech_prompts = self.encodec.quantizer.decode(speech_prompts.permute(1, 0, 2))
        
        encodings = self.encoder(
            tokens= tokens,
            lengths= token_lengths
            )
        speech_prompts = self.speech_prompter(speech_prompts)

        encodings_expand, _, _, durations, f0s, latent_lengths = self.variance_block(
            encodings= encodings,
            encoding_lengths= token_lengths,
            speech_prompts= speech_prompts,
            )

        if not ddim_steps is None and ddim_steps < self.hp.Diffusion.Max_Step:
            latents = self.diffusion.DDIM(
                encodings= encodings_expand,
                lengths= latent_lengths,
                speech_prompts= speech_prompts,
                ddim_steps= ddim_steps
                )        
        else:
            latents = self.diffusion.DDPM(
                encodings= encodings_expand,
                lengths= latent_lengths,
                speech_prompts= speech_prompts,
                )
        latents = latents * self.latent_std + self.latent_mean
        
        # Performing VQ to correct the incomplete predictions of diffusion.
        latents = self.encodec.quantizer.encode(
            x= latents,
            sample_rate= self.encodec.frame_rate,
            bandwidth= self.encodec.bandwidth
            )
        latents = self.encodec.quantizer.decode(latents)
        predictions = self.encodec.decoder(latents).squeeze(1)  # [Batch, Audio_t]

        return \
            predictions, None, None, None, None, \
            None, None, None, \
            None, None, None, durations, f0s, latent_lengths

    def train(self, mode: bool= True):
        super().train(mode= mode)
        self.encodec.eval() # encodec is always eval mode.

class Phoneme_Encoder(torch.nn.Module): 
    def __init__(
        self,
        hyper_parameters: Namespace
        ):
        super().__init__()
        self.hp = hyper_parameters

        self.token_embedding = torch.nn.Embedding(
            num_embeddings= self.hp.Tokens,
            embedding_dim= self.hp.Encoder.Size,
            )
        embedding_variance = math.sqrt(3.0) * math.sqrt(2.0 / (self.hp.Tokens + self.hp.Encoder.Size))
        self.token_embedding.weight.data.uniform_(-embedding_variance, embedding_variance)

        self.blocks = torch.nn.ModuleList([
            FFT_Block(
                channels= self.hp.Encoder.Size,
                num_head= self.hp.Encoder.Transformer.Head,
                feedforward_kernel_size= self.hp.Encoder.Transformer.FFN.Kernel_Size,
                feedforward_dropout_rate= self.hp.Encoder.Transformer.FFN.Dropout_Rate,
                )
            for index in range(self.hp.Encoder.Transformer.Stack)
            ])

    def forward(
        self,
        tokens: torch.Tensor,
        lengths: torch.Tensor,
        ) -> torch.Tensor:
        '''
        tokens: [Batch, Time]
        '''
        encodings = self.token_embedding(tokens).permute(0, 2, 1)

        for block in self.blocks:
            encodings = block(encodings, lengths)
        
        return encodings

class FFT_Block(torch.nn.Module):
    def __init__(
        self,
        channels: int,
        num_head: int,
        feedforward_kernel_size: int,
        feedforward_dropout_rate: float= 0.2
        ) -> None:
        super().__init__()

        self.attention = LinearAttention(
            query_channels= channels,
            key_channels= channels, 
            value_channels= channels,
            calc_channels= channels,
            num_heads= num_head
            )
        
        self.ffn = FFN(
            channels= channels,
            kernel_size= feedforward_kernel_size,
            dropout_rate= feedforward_dropout_rate
            )
        
    def forward(
        self,
        x: torch.Tensor,
        lengths: torch.Tensor,
        ) -> torch.Tensor:
        '''
        x: [Batch, Dim, Time]
        '''
        masks = Mask_Generate(lengths= lengths, max_length= torch.ones_like(x[0, 0]).sum())   # [Batch, Time]

        # Attention + Dropout + Residual + Norm
        x = self.attention(
            queries= x,
            keys= x,
            values= x,
            key_padding_masks= masks
            )

        # FFN + Dropout + Norm
        float_masks = (~masks).unsqueeze(1).float()   # float mask
        x = self.ffn(x, float_masks)

        return x * float_masks

class FFN(torch.nn.Module):
    def __init__(
        self,
        channels: int,
        kernel_size: int,
        dropout_rate: float= 0.1,
        ) -> None:
        super().__init__()
        self.conv_0 = Conv1d(
            in_channels= channels,
            out_channels= channels * 4,
            kernel_size= kernel_size,
            padding= (kernel_size - 1) // 2,
            w_init_gain= 'relu'
            )
        self.silu = torch.nn.SiLU()
        self.norm_0 = RMSNorm(
            num_features= channels * 4,
            )
        self.dropout = torch.nn.Dropout(p= dropout_rate)
        self.conv_1 = Conv1d(
            in_channels= channels * 4,
            out_channels= channels,
            kernel_size= kernel_size,
            padding= (kernel_size - 1) // 2,
            w_init_gain= 'linear'
            )
        self.norm_1 = RMSNorm(
            num_features= channels,
            )
        
    def forward(
        self,
        x: torch.Tensor,
        masks: torch.Tensor
        ) -> torch.Tensor:
        '''
        x: [Batch, Dim, Time]
        '''
        residuals = x

        x = self.conv_0(x * masks)
        x = self.silu(x)
        x = self.norm_0(x)
        x = self.dropout(x)
        x = self.conv_1(x * masks)
        x = self.dropout(x)
        x = self.norm_1(x + residuals)

        return x * masks


class Speech_Prompter(torch.nn.Module):
    def __init__(
        self,
        hyper_parameters: Namespace
        ):
        super().__init__()
        self.hp = hyper_parameters

        self.prenet = torch.nn.Sequential(
            Conv1d(
                in_channels= self.hp.Audio_Codec.Size,
                out_channels= self.hp.Speech_Prompter.Size,
                kernel_size= 1,
                w_init_gain= 'relu'
                ),
            RMSNorm(num_features= self.hp.Speech_Prompter.Size),
            torch.nn.SiLU()
            )
        
        self.blocks = torch.nn.ModuleList([
            FFT_Block(
                channels= self.hp.Speech_Prompter.Size,
                num_head= self.hp.Speech_Prompter.Transformer.Head,
                feedforward_kernel_size= self.hp.Speech_Prompter.Transformer.FFN.Kernel_Size,
                feedforward_dropout_rate= self.hp.Speech_Prompter.Transformer.FFN.Dropout_Rate,
                )
            for index in range(self.hp.Speech_Prompter.Transformer.Stack)
            ])

    def forward(
        self,
        speech_prompts: torch.Tensor,
        ) -> torch.Tensor:
        '''
        speech_prompts: [Batch, Dim, Time]
        '''
        lengths = torch.full(
            size= (speech_prompts.size(0),),
            fill_value= speech_prompts.size(2),
            dtype= torch.long,
            device= speech_prompts.device
            )

        speech_prompts = self.prenet(speech_prompts)

        for block in self.blocks:
            speech_prompts = block(
                x= speech_prompts,
                lengths= lengths
                )
        
        return speech_prompts


class Variacne_Block(torch.nn.Module):
    def __init__(
        self,
        hyper_parameters: Namespace
        ):
        super().__init__()
        self.hp = hyper_parameters

        self.duration_predictor = Duration_Predictor(self.hp)
        self.f0_predictor = F0_Predictor(self.hp)

        self.f0_embedding = Conv1d(
            in_channels= 1,
            out_channels= self.hp.Encoder.Size,
            kernel_size= 1,
            w_init_gain= 'linear'
            )
        
    def forward(
        self,
        encodings: torch.FloatTensor,
        encoding_lengths: torch.LongTensor,
        speech_prompts: torch.FloatTensor,
        durations: Optional[torch.LongTensor]= None,
        f0s: Optional[torch.FloatTensor]= None,
        latent_lengths: Optional[torch.LongTensor]= None,
        ):
        duration_predictions = self.duration_predictor(
            encodings= encodings,
            lengths= encoding_lengths,
            speech_prompts= speech_prompts
            )   # [Batch, Enc_t]
                
        if durations is None:
            durations = duration_predictions.ceil().long() # [Batch, Enc_t]
            latent_lengths = torch.stack([
                duration[:length - 1].sum() + 1
                for duration, length in zip(durations, encoding_lengths)
                ], dim= 0)
            max_duration_sum = latent_lengths.max()

            for duration, length in zip(durations, encoding_lengths):
                duration[length - 1:] = 0
                duration[length - 1] = max_duration_sum - duration.sum()

        alignments = self.Length_Regulate(durations= durations)
        encodings = encodings @ alignments  # [Batch, Enc_d, Latent_t]

        f0_predictions = self.f0_predictor(
            encodings= encodings,
            lengths= latent_lengths,
            speech_prompts= speech_prompts
            )   # [Batch, Latent_t]

        if f0s is None:
            f0s = f0_predictions

        encodings = encodings + self.f0_embedding(f0s.unsqueeze(1))  # [Batch, Enc_d, Latent_t]

        return encodings, duration_predictions, f0_predictions, durations, f0s, latent_lengths
    
    def Length_Regulate(
        self,
        durations: torch.LongTensor
        ) -> torch.FloatTensor:
        repeats = (durations.float() + 0.5).long()
        decoding_lengths = repeats.sum(dim=1)

        max_decoding_length = decoding_lengths.max()
        reps_cumsum = torch.cumsum(torch.nn.functional.pad(repeats, (1, 0, 0, 0), value=0.0), dim=1)[:, None, :]

        range_ = torch.arange(max_decoding_length)[None, :, None].to(durations.device)
        alignments = (reps_cumsum[:, :, :-1] <= range_) & (reps_cumsum[:, :, 1:] > range_)
        
        return alignments.permute(0, 2, 1).float()

class Variance_Predictor(torch.nn.Module): 
    def __init__(
        self,
        channels: int,
        condition_channels: int,
        stack: int,
        attention_num_head: int,        
        conv_kernel_size: int,
        conv_stack_in_stack: int,
        conv_dropout_rate: float
        ):
        super().__init__()
        
        self.conv_blocks = torch.nn.ModuleList()
        for index in range(stack):
            conv_block = torch.nn.ModuleList()
            for conv_block_index in range(conv_stack_in_stack):
                conv = torch.nn.Sequential()
                conv.append(Conv1d(
                    in_channels= channels,
                    out_channels= channels,
                    kernel_size= conv_kernel_size,
                    padding= (conv_kernel_size - 1) // 2,
                    w_init_gain= 'relu'
                    ))
                conv.append(RMSNorm(num_features= channels))
                conv.append(torch.nn.SiLU())
                conv.append(torch.nn.Dropout(p= conv_dropout_rate))
                conv_block.append(conv)
            self.conv_blocks.append(conv_block)

        self.attentions = torch.nn.ModuleList([
            LinearAttention(
                query_channels= channels,
                key_channels= condition_channels, 
                value_channels= condition_channels,
                calc_channels= channels,
                num_heads= attention_num_head,
                )
            for index in range(stack)
            ])

        self.projection = Conv1d(
            in_channels= channels,
            out_channels= 1,
            kernel_size= conv_kernel_size,
            padding= (conv_kernel_size - 1) // 2,
            w_init_gain= 'linear'
            )

    def forward(
        self,
        encodings: torch.Tensor,
        lengths: torch.Tensor,
        speech_prompts: torch.Tensor,
        ) -> torch.Tensor:
        '''
        encodings: [Batch, Enc_d, Enc_t or Feature_t]
        speech_prompts: [Batch, Enc_d, Prompt_t]
        '''
        masks = (~Mask_Generate(lengths= lengths, max_length= torch.ones_like(encodings[0, 0]).sum())).unsqueeze(1).float()   # float mask, [Batch, 1, Enc_t]
        x = encodings

        for conv_blocks, attention in zip(self.conv_blocks, self.attentions):
            for conv_block in conv_blocks:
                x = conv_block(x * masks) + x

            # Attention + Dropout + Residual + Norm
            x = attention(
                queries= x,
                keys= speech_prompts,
                values= speech_prompts
                )

        x = self.projection(x * masks) * masks

        return x.squeeze(1)

class Duration_Predictor(Variance_Predictor):
    def __init__(
        self,
        hyper_parameters: Namespace,
        ):
        self.hp = hyper_parameters        
        super().__init__(
            channels= self.hp.Encoder.Size,
            condition_channels= self.hp.Speech_Prompter.Size,
            stack= self.hp.Duration_Predictor.Stack,
            attention_num_head= self.hp.Duration_Predictor.Attention.Head,
            conv_kernel_size= self.hp.Duration_Predictor.Conv.Kernel_Size,
            conv_stack_in_stack= self.hp.Duration_Predictor.Conv.Stack,
            conv_dropout_rate= self.hp.Duration_Predictor.Conv.Dropout_Rate,
            )
    
    def forward(
        self,
        encodings: torch.Tensor,
        lengths: torch.Tensor,
        speech_prompts: torch.Tensor
        ) -> torch.Tensor:
        '''
        encodings: [Batch, Enc_d, Enc_t or Feature_t]
        speech_prompts: [Batch, Enc_d, Prompt_t]
        '''
        durations = super().forward(
            encodings= encodings,
            lengths= lengths,
            speech_prompts= speech_prompts
            )
        return torch.nn.functional.softplus(durations)

class F0_Predictor(Variance_Predictor):
    def __init__(
        self,
        hyper_parameters: Namespace,
        ):
        self.hp = hyper_parameters
        super().__init__(
            channels= self.hp.Encoder.Size,
            condition_channels= self.hp.Speech_Prompter.Size,
            stack= self.hp.Duration_Predictor.Stack,
            attention_num_head= self.hp.Duration_Predictor.Attention.Head,
            conv_kernel_size= self.hp.Duration_Predictor.Conv.Kernel_Size,
            conv_stack_in_stack= self.hp.Duration_Predictor.Conv.Stack,
            conv_dropout_rate= self.hp.Duration_Predictor.Conv.Dropout_Rate,
            )


class Segment(torch.nn.Module):
    def forward(
        self,
        patterns: torch.Tensor,
        segment_size: int,
        lengths: torch.Tensor= None,
        offsets: torch.Tensor= None
        ):
        '''
        patterns: [Batch, Time, ...]
        lengths: [Batch]
        segment_size: an integer scalar    
        '''
        if offsets is None:
            offsets = (torch.rand_like(patterns[:, 0, 0]) * (lengths - segment_size)).long()
        segments = torch.stack([
            pattern[offset:offset + segment_size]
            for pattern, offset in zip(patterns, offsets)
            ], dim= 0)
        
        return segments, offsets

def Mask_Generate(lengths: torch.Tensor, max_length: Optional[Union[int, torch.Tensor]]= None):
    '''
    lengths: [Batch]
    max_lengths: an int value. If None, max_lengths == max(lengths)
    '''
    max_length = max_length or torch.max(lengths)
    sequence = torch.arange(max_length)[None, :].to(lengths.device)
    return sequence >= lengths[:, None]    # [Batch, Time]

class CE_RVQ(torch.nn.Module):
    def __init__(
        self,
        encodec: EncodecModel,
        rvq_sample: int= 4,
        use_weighted_sample: bool= True
        ):
        super().__init__()
        self.encodec = encodec
        self.num_vq = encodec.quantizer.n_q
        self.rvq_sample = rvq_sample
        self.use_weighted_sample = use_weighted_sample

    def forward(
        self,
        diffusion_starts: torch.FloatTensor,
        target_latent_codes: torch.LongTensor
        ):
        '''
        diffusion_starts: [Batch, Latent_d, Latent_t]
        target_latent_codes: [Batch, Num_VQ, Latent_t]
        '''
        if self.use_weighted_sample:
            sample_rvq_indices = sorted(np.random.choice(
                range(self.num_vq),
                p= np.arange(self.num_vq, 0, -1) / sum(range(1, self.num_vq + 1)),
                size= self.rvq_sample,
                replace= False,
                ))
        else:
            sample_rvq_indices = sample(range(self.num_vq), self.rvq_sample)
        
        residuals = diffusion_starts
        loss_list = []
        for vq_index, (layer, latent_codes) in enumerate(zip(self.encodec.quantizer.vq.layers, target_latent_codes.permute(1, 0, 2))):
            x = rearrange(residuals, 'batch latent_d latent_t -> batch latent_t latent_d')            
            x = layer.project_in(x) # but, project_in == torch.nn.Identity.            
            quantizations, _ = layer._codebook(x)
            quantizations = layer.project_out(quantizations)   # but, project_out == torch.nn.Identity.
            quantizations = rearrange(quantizations, 'batch latent_t latent_d -> batch latent_d latent_t')   # [Batch, Latent_t, Latent_d]
            residuals = residuals - quantizations.detach()

            if vq_index in sample_rvq_indices:
                x = rearrange(x, 'batch latent_t latent_d -> batch 1 latent_t latent_d')
                codebooks = rearrange(layer._codebook.embed, 'num_codebook latent_d -> 1 num_codebook 1 latent_d')
                logits = -(x - codebooks.detach()).pow(2.0).mean(dim= 3) # [Batch, Num_Codebook, Latent_t]
                loss_list.append(torch.nn.functional.cross_entropy(logits, latent_codes, reduction='mean'))
                
                # loss_list.append(torch.nn.functional.mse_loss(
                #     x,
                #     layer._codebook.embed[latent_codes].detach(),
                #     reduction= 'mean'
                #     ))

        return torch.stack(loss_list).mean()