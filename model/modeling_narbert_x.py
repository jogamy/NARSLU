import torch.nn as nn
from transformers.modeling_bert import BertPreTrainedModel, BertModel, BertConfig
from torchcrf import CRF
from .module import IntentClassifier, SlotClassifier, NonAutoregressiveWrapper
from x_transformers.x_transformers import *


class NARBERTX(BertPreTrainedModel):
    def __init__(self, config, args, intent_label_lst, slot_label_lst):
        super(NARBERTX, self).__init__(config)
        self.args = args
        self.num_intent_labels = len(intent_label_lst)
        self.num_slot_labels = len(slot_label_lst)
        bslot_label_lst = [token for token in slot_label_lst if "I-" not in token]
        islot_label_lst = [token for token in slot_label_lst if "B-" not in token]

        self.num_bslot_labels = len(bslot_label_lst)
        self.num_islot_labels = len(islot_label_lst)
        self.mask_id = slot_label_lst.index("MASK")

        self.bert = BertModel(config=config)  # Load pretrained bert

        self.intent_classifier = None
        if self.args.task in ["atis", "snips"]:
            self.intent_classifier = IntentClassifier(
                config.hidden_size, self.num_intent_labels, args.dropout_rate
            )
        # self.slot_classifier = SlotClassifier(
        #     config.hidden_size, self.num_slot_labels, args.dropout_rate
        # )

        self.slot_classifier = None
        self.bslot_classifier = None
        self.islot_classifier = None
        if args.n_dec == 1:
            transformer_kwargs = {
                "num_tokens": self.num_slot_labels,
                "max_seq_len": args.max_seq_len,
                "emb_dropout": args.dropout_rate,
            }
            decoder_kwargs = {
                "depth": 1,
                "dropout": args.dropout_rate,
                "heads": 8,
                "layer_dropout": args.dropout_rate,
            }

            decoder = TransformerWrapper(
                **transformer_kwargs,
                attn_layers=Encoder(
                    dim=config.hidden_size, cross_attend=True, **decoder_kwargs
                ),
            )

            self.slot_classifier = NonAutoregressiveWrapper(
                decoder,
                mask_index=2,
                pad_value=0,
                max_seq_len=args.max_seq_len,
            )

        if args.n_dec == 2:
            transformer_kwargs = {
                "num_tokens": self.num_bslot_labels,
                "max_seq_len": args.max_seq_len,
                "emb_dropout": args.dropout_rate,
            }
            decoder_kwargs = {
                "depth": 1,
                "dropout": args.dropout_rate,
                "heads": 8,
                "layer_dropout": args.dropout_rate,
            }

            decoder = TransformerWrapper(
                **transformer_kwargs,
                attn_layers=Encoder(
                    dim=config.hidden_size, cross_attend=True, **decoder_kwargs
                ),
            )

            self.bslot_classifier = NonAutoregressiveWrapper(
                decoder,
                mask_index=2,
                pad_value=0,
                max_seq_len=args.max_seq_len,
            )

            transformer_kwargs = {
                "num_tokens": self.num_islot_labels,
                "max_seq_len": args.max_seq_len,
                "emb_dropout": args.dropout_rate,
            }
            decoder_kwargs = {
                "depth": 1,
                "dropout": args.dropout_rate,
                "heads": 8,
                "layer_dropout": args.dropout_rate,
            }

            decoder = TransformerWrapper(
                **transformer_kwargs,
                attn_layers=Encoder(
                    dim=config.hidden_size, cross_attend=True, **decoder_kwargs
                ),
            )

            self.islot_classifier = NonAutoregressiveWrapper(
                decoder,
                mask_index=2,
                pad_value=0,
                max_seq_len=args.max_seq_len,
            )

        if args.use_crf:
            self.crf = CRF(num_tags=self.num_slot_labels, batch_first=True)

    def forward(
        self,
        input_ids,
        attention_mask,
        token_type_ids,
        intent_label_ids,
        slot_labels_ids,
        bslot_labels_ids,
        islot_labels_ids,
    ):
        model_output = {}
        model_output["slot_labels_ids"] = slot_labels_ids
        outputs = self.bert(
            input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids
        )  # sequence_output, pooled_output, (hidden_states), (attentions)
        sequence_output = outputs[0]
        pooled_output = outputs[1]  # [CLS]

        intent_logits = self.intent_classifier(pooled_output)
        model_output["intent_logits"] = intent_logits

        if self.args.n_dec == 1:
            slot_logits = self.slot_classifier(
                x=slot_labels_ids,
                context=sequence_output,
                context_mask=attention_mask.bool(),
            )
            model_output["slot_logits"] = slot_logits
            model_output["bslot_logits"] = None
            model_output["islot_logits"] = None
        elif self.args.n_dec == 2:
            bslot_logits = self.bslot_classifier(
                x=bslot_labels_ids,
                context=sequence_output,
                context_mask=attention_mask.bool(),
            )
            islot_logits = self.islot_classifier(
                x=islot_labels_ids,
                context=sequence_output,
                context_mask=attention_mask.bool(),
            )
            model_output["slot_logits"] = None
            model_output["bslot_logits"] = bslot_logits
            model_output["islot_logits"] = islot_logits

        model_output["loss"] = 0
        # 1. Intent Softmax
        if intent_label_ids is not None:
            if self.num_intent_labels == 1:
                intent_loss_fct = nn.MSELoss()
                intent_loss = intent_loss_fct(
                    intent_logits.view(-1), intent_label_ids.view(-1)
                )
            else:
                intent_loss_fct = nn.CrossEntropyLoss()
                intent_loss = intent_loss_fct(
                    intent_logits.view(-1, self.num_intent_labels),
                    intent_label_ids.view(-1),
                )
            model_output["loss"] += intent_loss

        # 2. Slot Softmax
        # if slot_labels_ids is not None and self.slot_classifier is not None:
        if self.args.n_dec == 1:
            if self.args.use_crf:
                slot_loss = self.crf(
                    slot_logits,
                    slot_labels_ids,
                    mask=attention_mask.byte(),
                    reduction="mean",
                )
                slot_loss = -1 * slot_loss  # negative log-likelihood
            else:
                slot_loss_fct = nn.CrossEntropyLoss(ignore_index=self.args.ignore_index)
                # Only keep active parts of the loss
                if attention_mask is not None:
                    active_loss = attention_mask.view(-1) == 1
                    # print(slot_logits.shape)
                    # print(attention_mask.shape)
                    # assert 1 == 0
                    active_logits = slot_logits.view(-1, self.num_slot_labels)[
                        active_loss
                    ]
                    active_labels = slot_labels_ids.view(-1)[active_loss]
                    slot_loss = slot_loss_fct(active_logits, active_labels)
                else:
                    slot_loss = slot_loss_fct(
                        slot_logits.view(-1, self.num_slot_labels),
                        slot_labels_ids.view(-1),
                    )
            model_output["loss"] += slot_loss
            # total_loss += self.args.slot_loss_coef * slot_loss
            # outputs = ((intent_logits, slot_logits),) + outputs[
            #     2:
            # ]  # add hidden states and attention if they are here

            # outputs = (total_loss,) + outputs
            return model_output

            # return outputs  # (loss), logits, (hidden_states), (attentions) # Logits is a tuple of intent and slot logits
        else:
            assert bslot_labels_ids is not None
            assert islot_labels_ids is not None

            bslot_loss_fct = nn.CrossEntropyLoss(ignore_index=self.args.ignore_index)

            bslot_loss = bslot_loss_fct(
                bslot_logits.view(-1, self.num_bslot_labels),
                bslot_labels_ids.view(-1),
            )

            islot_loss_fct = nn.CrossEntropyLoss(ignore_index=self.args.ignore_index)
            islot_loss = islot_loss_fct(
                islot_logits.view(-1, self.num_islot_labels),
                islot_labels_ids.view(-1),
            )

            slot_loss = bslot_loss + islot_loss

            model_output["loss"] += slot_loss

            return model_output

            # outputs = ((intent_logits, bslot_logits, islot_logits),) + outputs[
            #     2:
            # ]  # add hidden states and attention if they are here

            # outputs = (total_loss,) + outputs

            # return outputs
