from argparse import Namespace
import torch
import math
from typing import Optional, Union
from encodec import EncodecModel

from .MAS import Monotonic_Alignment_Search
from .Diffusion import Diffusion
from .LinearAttention import LinearAttention
from .Layer import Conv1d, LayerNorm


class NaturalSpeech2(torch.nn.Module):
    def __init__(
        self,
        hyper_parameters: Namespace,
        latent_min: float,
        latent_max: float,
        ):
        super().__init__()
        self.hp = hyper_parameters
        self.latent_min= latent_min
        self.latent_max= latent_max

        self.encoder = Phoneme_Encoder(self.hp)
        self.speech_prompter = Speech_Prompter(self.hp)

        self.monotonic_alignment_search = Monotonic_Alignment_Search(
            in_channels= self.hp.Encoder.Size,
            feature_size= self.hp.Sound.Mel_Dim
            )

        self.variance_block = Variacne_Block(self.hp)

        self.diffusion = Diffusion(self.hp)

        self.encodec = EncodecModel.encodec_model_24khz()
        
        self.segment = Segment()

    def forward(
        self,
        tokens: torch.LongTensor,
        token_lengths: torch.LongTensor,
        speech_prompts: torch.FloatTensor,
        speech_prompts_for_diffusion: Optional[torch.FloatTensor]= None,
        latents: Optional[torch.FloatTensor]= None,
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
        speech_prompts: torch.FloatTensor,
        speech_prompts_for_diffusion: torch.FloatTensor,
        latents: torch.FloatTensor,
        latent_lengths: torch.LongTensor,
        f0s: torch.FloatTensor,
        mels: torch.Tensor,
        attention_priors: torch.Tensor,
        ):
        encodings = self.encoder(
            tokens= tokens,
            lengths= token_lengths
            )
        speech_prompts = self.speech_prompter(speech_prompts)
        speech_prompts_for_diffusion = self.speech_prompter(speech_prompts_for_diffusion)

        alignments, durations, mas_means, mas_log_stds = self.monotonic_alignment_search(
            encodings= encodings,
            encoding_lengths= token_lengths,
            features= mels,
            feature_lengths= latent_lengths
            )

        encodings_expand, mas_means, mas_log_stds, duration_predictions, f0_predictions, _, _, _ = self.variance_block(
            encodings= encodings,
            encoding_lengths= token_lengths,
            speech_prompts= speech_prompts,
            mas_means= mas_means,
            mas_log_stds= mas_log_stds,
            alignments= alignments,
            f0s= f0s,
            latent_lengths= latent_lengths
            )
        
        encodings_expand_slice, offsets = self.segment(
            patterns= encodings_expand.permute(0, 2, 1),
            segment_size= self.hp.Train.Segment_Size,
            lengths= latent_lengths
            )        
        encodings_expand_slice = encodings_expand_slice.permute(0, 2, 1)
        
        latents_slice, _ = self.segment(
            patterns= latents.permute(0, 2, 1),
            segment_size= self.hp.Train.Segment_Size,
            offsets= offsets
            )
        latents_slice = latents_slice.permute(0, 2, 1)

        _, noises, epsilons = self.diffusion(
            encodings= encodings_expand_slice,
            lengths= torch.full_like(latent_lengths, fill_value= self.hp.Train.Segment_Size),
            speech_prompts= speech_prompts_for_diffusion,
            latents= latents_slice
            )

        return \
            None, noises, epsilons, duration_predictions, f0_predictions, \
            mas_means, mas_log_stds, durations, None, None, None

    def Inference(
        self,
        tokens: torch.LongTensor,
        token_lengths: torch.LongTensor,
        speech_prompts: torch.FloatTensor,
        ddim_steps: Optional[int]= None
        ):
        encodings = self.encoder(
            tokens= tokens,
            lengths= token_lengths
            )
        speech_prompts = self.speech_prompter(speech_prompts)

        encodings_expand, *_, alignments, f0s, latent_lengths = self.variance_block(
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
            latents, _, _ = self.diffusion(
                encodings= encodings_expand,
                lengths= latent_lengths,
                speech_prompts= speech_prompts,
                )
        
        latents = (latents + 1.0) / 2.0 * (self.latent_max - self.latent_min) + self.latent_min

        # Performing VQ to correct the predictions of incomplete diffusion.        
        latents = self.encodec.quantizer.encode(
            x= latents,
            sample_rate= self.encodec.frame_rate,
            bandwidth= self.encodec.bandwidth
            )
        latents = self.encodec.quantizer.decode(latents)
        predictions = self.encodec.decoder(latents).squeeze(1)  # [Batch, Audio_t]

        return \
            predictions, None, None, None, None, \
            None, None, None, None, alignments, f0s, latent_lengths

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
                dropout_rate= self.hp.Encoder.Transformer.Dropout_Rate,
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
        dropout_rate: float= 0.0,
        feedforward_dropout_rate: float= 0.2
        ) -> None:
        super().__init__()

        self.attention = LinearAttention(
            query_channels= channels,
            key_channels= channels, 
            value_channels= channels,
            calc_channels= channels,
            num_heads= num_head,
            dropout_rate= dropout_rate
            )
        
        self.ffn = FFN(
            channels= channels,
            kernel_size= feedforward_kernel_size,
            dropout_rate= feedforward_dropout_rate
            )
        
    def forward(
        self,
        x: torch.Tensor,
        lengths: torch.Tensor
        ) -> torch.Tensor:
        '''
        x: [Batch, Dim, Time]
        '''
        masks = Mask_Generate(lengths= lengths, max_length= torch.ones_like(x[0, 0]).sum())   # [Batch, Time]

        # Attention + Dropout + Residual + LayerNorm
        x = self.attention(
            queries= x,
            keys= x,
            values= x,
            key_padding_masks= masks
            )
        
        # FFN + Dropout + LayerNorm
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
        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p= dropout_rate)
        self.conv_1 = Conv1d(
            in_channels= channels * 4,
            out_channels= channels,
            kernel_size= kernel_size,
            padding= (kernel_size - 1) // 2,
            w_init_gain= 'linear'
            )
        self.norm = LayerNorm(
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
        x = self.relu(x)
        x = self.dropout(x)
        x = self.conv_1(x * masks)
        x = self.dropout(x)
        x = self.norm(x + residuals)

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
            LayerNorm(num_features= self.hp.Speech_Prompter.Size),
            torch.nn.ReLU()
            )
        
        self.blocks = torch.nn.ModuleList([
            FFT_Block(
                channels= self.hp.Speech_Prompter.Size,
                num_head= self.hp.Speech_Prompter.Transformer.Head,
                feedforward_kernel_size= self.hp.Speech_Prompter.Transformer.FFN.Kernel_Size,
                dropout_rate= self.hp.Speech_Prompter.Transformer.Dropout_Rate,
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
        mas_means: Optional[torch.FloatTensor]= None,
        mas_log_stds: Optional[torch.FloatTensor]= None,
        alignments: Optional[torch.FloatTensor]= None,
        f0s: Optional[torch.FloatTensor]= None,
        latent_lengths: Optional[torch.LongTensor]= None,
        ):
        duration_predictions = self.duration_predictor(
            encodings= encodings,
            lengths= encoding_lengths,
            speech_prompts= speech_prompts
            )   # [Batch, Enc_t]
        
        if alignments is None:
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

        encodings = encodings @ alignments # [Batch, Enc_d, Latent_t]

        if not mas_means is None:
            mas_means = mas_means @ alignments
        if not mas_log_stds is None:
            mas_log_stds = mas_log_stds @ alignments

        f0_predictions = self.f0_predictor(
            encodings= encodings,
            lengths= latent_lengths,
            speech_prompts= speech_prompts
            )   # [Batch, Latent_t]

        if f0s is None:
            f0s = f0_predictions

        encodings = encodings + self.f0_embedding(f0s.unsqueeze(1))  # [Batch, Enc_d, Latent_t]

        return encodings, mas_means, mas_log_stds, duration_predictions, f0_predictions, alignments, f0s, latent_lengths
    
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
        stack: int,
        attention_num_head: int,
        attention_dropout_rate: float,        
        conv_kernel_size: int,
        conv_stack_in_stack: int,
        conv_dropout_rate: float
        ):
        super().__init__()
        
        self.conv_blocks = torch.nn.ModuleList()
        for index in range(stack):
            conv_block = torch.nn.Sequential()
            for conv_block_index in range(conv_stack_in_stack):
                conv_block.append(Conv1d(
                    in_channels= channels,
                    out_channels= channels,
                    kernel_size= conv_kernel_size,
                    padding= (conv_kernel_size - 1) // 2,
                    w_init_gain= 'relu'
                    ))
                conv_block.append(LayerNorm(num_features= channels))
                conv_block.append(torch.nn.ReLU())
                conv_block.append(torch.nn.Dropout(p= conv_dropout_rate))
                self.conv_blocks.append(conv_block)

        self.attentions = torch.nn.ModuleList([
            LinearAttention(
                query_channels= channels,
                key_channels= channels, 
                value_channels= channels,
                calc_channels= channels,
                num_heads= attention_num_head,
                dropout_rate= attention_dropout_rate
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
        speech_prompts: torch.Tensor
        ) -> torch.Tensor:
        '''
        encodings: [Batch, Enc_d, Enc_t or Feature_t]
        speech_prompts: [Batch, Enc_d, Prompt_t]
        '''
        masks = (~Mask_Generate(lengths= lengths, max_length= torch.ones_like(encodings[0, 0]).sum())).unsqueeze(1).float()   # float mask, [Batch, 1, Enc_t]
        x = encodings

        for conv_blocks, attention in zip(self.conv_blocks, self.attentions):
            x = conv_blocks(x * masks) + x

            # Attention + Dropout + Residual + LayerNorm
            x = attention(
                queries= x * masks,
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
            stack= self.hp.Duration_Predictor.Stack,
            attention_num_head= self.hp.Duration_Predictor.Attention.Head,
            attention_dropout_rate= self.hp.Duration_Predictor.Attention.Dropout_Rate,
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
            stack= self.hp.Duration_Predictor.Stack,
            attention_num_head= self.hp.Duration_Predictor.Attention.Head,
            attention_dropout_rate= self.hp.Duration_Predictor.Attention.Dropout_Rate,
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
