//go:build can_inbound_probe

package whatsapp

import "fmt"

// Compiled only into the offline smoke library. Never included in the deployed binary.
func (s *Session) canInboundOfflineProbe() {
    d:=s.canGetInbound()
    d.enqueue(d.history,func(){s.canDeliverInbound(EventMessage,&EventPayload{Message:Message{ID:"history",Body:"history",IsHistory:true}})},false)
    d.enqueue(d.control,func(){s.canDeliverInbound(EventConnect,&EventPayload{Connect:Connect{JID:"fixture@example.invalid"}})},false)
    go func(){
        for i:=0;i<100;i++ {
            n:=i
            d.enqueue(d.live,func(){s.canDeliverInbound(EventMessage,&EventPayload{Message:Message{ID:fmt.Sprint(n),Body:"fixture"}})},false)
        }
    }()
}
